# Licensed under a 3-clause BSD style license - see LICENSE.rst
"""Fermi catalog and source classes."""

import abc
import logging
import warnings
import numpy as np
import astropy.units as u
from astropy.table import Table
from astropy.wcs import FITSFixedWarning
from gammapy.estimators import FluxPoints
from gammapy.maps import MapAxis, Maps, RegionGeom, RegionNDMap
from gammapy.modeling.models import (
    DiskSpatialModel,
    GaussianSpatialModel,
    Model,
    Models,
    PointSpatialModel,
    SkyModel,
    TemplateSpatialModel,
)
from gammapy.utils.gauss import Gauss2DPDF
from gammapy.utils.scripts import make_path
from gammapy.utils.table import table_standardise_units_inplace
from .core import SourceCatalog, SourceCatalogObject, format_flux_points_table

__all__ = [
    "SourceCatalog2FHL",
    "SourceCatalog3FGL",
    "SourceCatalog3FHL",
    "SourceCatalog4FGL",
    "SourceCatalog2PC",
    "SourceCatalog3PC",
    "SourceCatalogObject2FHL",
    "SourceCatalogObject3FGL",
    "SourceCatalogObject3FHL",
    "SourceCatalogObject4FGL",
    "SourceCatalogObject2PC",
    "SourceCatalogObject3PC",
]

log = logging.getLogger(__name__)


def get_nonentry_keys(d, keys):
    vals = [str(d[_]).strip() for _ in keys]
    return ", ".join([_ for _ in vals if _ not in ["", "--"]])


def get_nonentry_key(key):
    if key.strip() == "":
        return "--"
    else:
        return key


def compute_flux_points_ul(quantity, quantity_errp):
    """Compute UL value for fermi flux points.

    See https://arxiv.org/pdf/1501.02003.pdf (page 30).
    """
    return 2 * quantity_errp + quantity


class SourceCatalogObjectFermiPCBase(SourceCatalogObject, abc.ABC):
    """Base class for Fermi-LAT Pulsar catalogs."""

    def __str__(self):
        return self.info()

    def info(self, info="all"):
        if info == "all":
            info = "basic,more,position,pulsar,spectral,lightcurve"

        ss = ""
        ops = info.split(",")
        if "basic" in ops:
            ss += self._info_basic()
        if "more" in ops:
            ss += self._info_more()
        if "pulsar" in ops:
            ss += self._info_pulsar()
        if "position" in ops:
            ss += self._info_position()
        if "spectral" in ops:
            ss += self._info_spectral_fit()
            ss += self._info_spectral_points()
        if "lightcurve" in ops:
            ss += self._info_phasogram()
        return ss

    def _info_basic(self):
        ss = "\n*** Basic info ***\n\n"
        ss += "Catalog row index (zero-based) : {}\n".format(self.row_index)
        ss += "{:<20s} : {}\n".format("Source name", self.name)
        return ss

    def _info_more(self):
        return ""

    def _info_pulsar(self):
        return "\n"

    def _info_position(self):
        source_pos = self.position
        ss = "\n*** Position info ***\n\n"
        ss += "{:<20s} : {:.3f}\n".format("RA", source_pos.ra)
        ss += "{:<20s} : {:.3f}\n".format("DEC", source_pos.dec)
        ss += "{:<20s} : {:.3f}\n".format("GLON", source_pos.galactic.l)
        ss += "{:<20s} : {:.3f}\n".format("GLAT", source_pos.galactic.b)
        return ss

    def _info_spectral_fit(self):
        return "\n"

    def _info_spectral_points(self):
        ss = "\n*** Spectral points ***\n\n"
        if self.flux_points_table is None:
            ss += "No spectral points available.\n"
            return ss
        lines = format_flux_points_table(self.flux_points_table).pformat(
            max_width=-1, max_lines=-1
        )
        ss += "\n".join(lines)
        ss += "\n"
        return ss

    def _info_phasogram(self):
        return ""

    def spatial_model(self):
        source_pos = self.position
        ra = source_pos.ra
        dec = source_pos.dec

        model = PointSpatialModel(lon_0=ra, lat_0=dec, frame="icrs")
        return model

    def sky_model(self, name=None):
        """Sky model (`~gammapy.modeling.models.SkyModel`)."""
        spectral_model = self.spectral_model()
        if spectral_model is None:
            return None

        if name is None:
            name = self.name

        return SkyModel(
            spatial_model=self.spatial_model(),
            spectral_model=spectral_model,
            name=name,
        )

    @property
    def flux_points(self):
        """Flux points (`~gammapy.estimators.FluxPoints`)."""
        if self.flux_points_table is None:
            return None

        return FluxPoints.from_table(
            table=self.flux_points_table,
            reference_model=self.sky_model(),
            format="gadf-sed",
        )

    @property
    def lightcurve(self):
        """Light-curve."""
        pass


class SourceCatalogObjectFermiBase(SourceCatalogObject, abc.ABC):
    """Base class for Fermi-LAT catalogs."""

    asso = ["ASSOC1", "ASSOC2", "ASSOC_TEV", "ASSOC_GAM1", "ASSOC_GAM2", "ASSOC_GAM3"]
    flux_points_meta = {
        "sed_type_init": "flux",
        "n_sigma": 1,
        "sqrt_ts_threshold_ul": 1,
        "n_sigma_ul": 2,
    }

    def __str__(self):
        return self.info()

    def info(self, info="all"):
        """Summary information string.

        Parameters
        ----------
        info : {'all', 'basic', 'more', 'position', 'spectral', 'lightcurve'}
            Comma separated list of options.
        """
        if info == "all":
            info = "basic,more,position,spectral,lightcurve"

        ss = ""
        ops = info.split(",")
        if "basic" in ops:
            ss += self._info_basic()
        if "more" in ops:
            ss += self._info_more()
        if "position" in ops:
            ss += self._info_position()
            if not self.is_pointlike:
                ss += self._info_morphology()
        if "spectral" in ops:
            ss += self._info_spectral_fit()
            ss += self._info_spectral_points()
        if "lightcurve" in ops:
            ss += self._info_lightcurve()
        return ss

    def _info_basic(self):
        d = self.data
        keys = self.asso
        ss = "\n*** Basic info ***\n\n"
        ss += "Catalog row index (zero-based) : {}\n".format(self.row_index)
        ss += "{:<20s} : {}\n".format("Source name", self.name)

        if "Extended_Source_Name" in d:
            ss += "{:<20s} : {}\n".format(
                "Extended name", get_nonentry_key(d["Extended_Source_Name"])
            )

        associations = get_nonentry_keys(d, keys)
        ss += "{:<16s} : {}\n".format("Associations", associations)
        try:
            ss += "{:<16s} : {:.3f}\n".format("ASSOC_PROB_BAY", d["ASSOC_PROB_BAY"])
            ss += "{:<16s} : {:.3f}\n".format("ASSOC_PROB_LR", d["ASSOC_PROB_LR"])
        except KeyError:
            pass
        try:
            ss += "{:<16s} : {}\n".format("Class1", get_nonentry_key(d["CLASS1"]))
        except KeyError:
            ss += "{:<16s} : {}\n".format("Class", get_nonentry_key(d["CLASS"]))
        try:
            ss += "{:<16s} : {}\n".format("Class2", get_nonentry_key(d["CLASS2"]))
        except KeyError:
            pass
        ss += "{:<16s} : {}\n".format("TeVCat flag", d.get("TEVCAT_FLAG", "N/A"))
        return ss

    @abc.abstractmethod
    def _info_more(self):
        pass

    def _info_position(self):
        d = self.data
        ss = "\n*** Position info ***\n\n"
        ss += "{:<20s} : {:.3f}\n".format("RA", d["RAJ2000"])
        ss += "{:<20s} : {:.3f}\n".format("DEC", d["DEJ2000"])
        ss += "{:<20s} : {:.3f}\n".format("GLON", d["GLON"])
        ss += "{:<20s} : {:.3f}\n".format("GLAT", d["GLAT"])

        ss += "\n"
        ss += "{:<20s} : {:.4f}\n".format("Semimajor (68%)", d["Conf_68_SemiMajor"])
        ss += "{:<20s} : {:.4f}\n".format("Semiminor (68%)", d["Conf_68_SemiMinor"])
        ss += "{:<20s} : {:.2f}\n".format("Position angle (68%)", d["Conf_68_PosAng"])
        ss += "{:<20s} : {:.4f}\n".format("Semimajor (95%)", d["Conf_95_SemiMajor"])
        ss += "{:<20s} : {:.4f}\n".format("Semiminor (95%)", d["Conf_95_SemiMinor"])
        ss += "{:<20s} : {:.2f}\n".format("Position angle (95%)", d["Conf_95_PosAng"])
        ss += "{:<20s} : {:.0f}\n".format("ROI number", d["ROI_num"])
        return ss

    def _info_morphology(self):
        e = self.data_extended
        ss = "\n*** Extended source information ***\n\n"
        ss += "{:<16s} : {}\n".format("Model form", e["Model_Form"])
        ss += "{:<16s} : {:.4f}\n".format("Model semimajor", e["Model_SemiMajor"])
        ss += "{:<16s} : {:.4f}\n".format("Model semiminor", e["Model_SemiMinor"])
        ss += "{:<16s} : {:.4f}\n".format("Position angle", e["Model_PosAng"])
        try:
            ss += "{:<16s} : {}\n".format("Spatial function", e["Spatial_Function"])
        except KeyError:
            pass
        ss += "{:<16s} : {}\n\n".format(
            "Spatial filename", get_nonentry_key(e["Spatial_Filename"])
        )
        return ss

    def _info_spectral_fit(self):
        return "\n"

    def _info_spectral_points(self):
        ss = "\n*** Spectral points ***\n\n"
        lines = format_flux_points_table(self.flux_points_table).pformat(
            max_width=-1, max_lines=-1
        )
        ss += "\n".join(lines)
        return ss

    def _info_lightcurve(self):
        return "\n"

    @property
    def is_pointlike(self):
        name = self.data["Extended_Source_Name"].strip()
        return name == "" or name.strip() == "--"

    # FIXME: this should be renamed `set_position_error`,
    # and `phi_0` isn't filled correctly, other parameters missing
    # see https://github.com/gammapy/gammapy/pull/2533#issuecomment-553329049
    def _set_spatial_errors(self, model):
        d = self.data

        if "Pos_err_68" in d:
            percent = 0.68
            semi_minor = d["Pos_err_68"]
            semi_major = d["Pos_err_68"]
            phi_0 = 0.0
        else:
            percent = 0.95
            semi_minor = d["Conf_95_SemiMinor"]
            semi_major = d["Conf_95_SemiMajor"]
            phi_0 = d["Conf_95_PosAng"]

        if np.isnan(phi_0):
            phi_0 = 0.0 * u.deg

        scale_1sigma = Gauss2DPDF().containment_radius(percent)
        lat_err = semi_major / scale_1sigma
        lon_err = semi_minor / scale_1sigma / np.cos(d["DEJ2000"])

        if "TemplateSpatialModel" not in model.tag:
            model.parameters["lon_0"].error = lon_err
            model.parameters["lat_0"].error = lat_err
            model.phi_0 = phi_0

    def sky_model(self, name=None):
        """Sky model as a `~gammapy.modeling.models.SkyModel` object."""
        if name is None:
            name = self.name

        return SkyModel(
            spatial_model=self.spatial_model(),
            spectral_model=self.spectral_model(),
            name=name,
        )

    @property
    def flux_points(self):
        """Flux points as a `~gammapy.estimators.FluxPoints` object."""
        return FluxPoints.from_table(
            table=self.flux_points_table,
            reference_model=self.sky_model(),
            format="gadf-sed",
        )


class SourceCatalogObject4FGL(SourceCatalogObjectFermiBase):
    """One source from the Fermi-LAT 4FGL catalog.

    Catalog is represented by `~gammapy.catalog.SourceCatalog4FGL`.
    """

    asso = [
        "ASSOC1",
        "ASSOC2",
        "ASSOC_TEV",
        "ASSOC_FGL",
        "ASSOC_FHL",
        "ASSOC_GAM1",
        "ASSOC_GAM2",
        "ASSOC_GAM3",
    ]

    def _info_more(self):
        d = self.data
        ss = "\n*** Other info ***\n\n"
        fmt = "{:<32s} : {:.3f}\n"
        ss += fmt.format("Significance (100 MeV - 1 TeV)", d["Signif_Avg"])
        ss += "{:<32s} : {:.1f}\n".format("Npred", d["Npred"])
        ss += "\n{:<20s} : {}\n".format("Other flags", d["Flags"])
        return ss

    def _info_spectral_fit(self):
        d = self.data
        spec_type = d["SpectrumType"].strip()

        ss = "\n*** Spectral info ***\n\n"

        ss += "{:<45s} : {}\n".format("Spectrum type", d["SpectrumType"])
        fmt = "{:<45s} : {:.3f}\n"
        ss += fmt.format("Detection significance (100 MeV - 1 TeV)", d["Signif_Avg"])

        if spec_type == "PowerLaw":
            tag = "PL"
        elif spec_type == "LogParabola":
            tag = "LP"
            ss += "{:<45s} : {:.4f} +- {:.5f}\n".format(
                "beta", d["LP_beta"], d["Unc_LP_beta"]
            )
            ss += "{:<45s} : {:.1f}\n".format("Significance curvature", d["LP_SigCurv"])

        elif spec_type == "PLSuperExpCutoff":
            tag = "PLEC"
            fmt = "{:<45s} : {:.4f} +- {:.4f}\n"
            if "PLEC_ExpfactorS" in d:
                ss += fmt.format(
                    "Exponential factor", d["PLEC_ExpfactorS"], d["Unc_PLEC_ExpfactorS"]
                )
            else:
                ss += fmt.format(
                    "Exponential factor", d["PLEC_Expfactor"], d["Unc_PLEC_Expfactor"]
                )
            ss += "{:<45s} : {:.4f} +- {:.4f}\n".format(
                "Super-exponential cutoff index",
                d["PLEC_Exp_Index"],
                d["Unc_PLEC_Exp_Index"],
            )
            ss += "{:<45s} : {:.1f}\n".format(
                "Significance curvature", d["PLEC_SigCurv"]
            )

        else:
            raise ValueError(f"Invalid spec_type: {spec_type!r}")

        ss += "{:<45s} : {:.0f} {}\n".format(
            "Pivot energy", d["Pivot_Energy"].value, d["Pivot_Energy"].unit
        )

        fmt = "{:<45s} : {:.3f} +- {:.3f}\n"
        if f"{tag}_ExpfactorS" in d:
            ss += fmt.format(
                "Spectral index", d[tag + "_IndexS"], d["Unc_" + tag + "_IndexS"]
            )
        else:
            ss += fmt.format(
                "Spectral index", d[tag + "_Index"], d["Unc_" + tag + "_Index"]
            )

        fmt = "{:<45s} : {:.3} +- {:.3} {}\n"
        ss += fmt.format(
            "Flux Density at pivot energy",
            d[tag + "_Flux_Density"].value,
            d["Unc_" + tag + "_Flux_Density"].value,
            "cm-2 MeV-1 s-1",
        )

        fmt = "{:<45s} : {:.3} +- {:.3} {}\n"
        ss += fmt.format(
            "Integral flux (1 - 100 GeV)",
            d["Flux1000"].value,
            d["Unc_Flux1000"].value,
            "cm-2 s-1",
        )

        fmt = "{:<45s} : {:.3} +- {:.3} {}\n"
        ss += fmt.format(
            "Energy flux (100 MeV - 100 GeV)",
            d["Energy_Flux100"].value,
            d["Unc_Energy_Flux100"].value,
            "erg cm-2 s-1",
        )

        return ss

    def _info_lightcurve(self):
        d = self.data
        ss = "\n*** Lightcurve info ***\n\n"
        ss += "Lightcurve measured in the energy band: 100 MeV - 100 GeV\n\n"

        ss += "{:<15s} : {:.3f}\n".format("Variability index", d["Variability_Index"])

        if np.isfinite(d["Flux_Peak"]):
            ss += "{:<40s} : {:.3f}\n".format(
                "Significance peak (100 MeV - 100 GeV)", d["Signif_Peak"]
            )

            fmt = "{:<40s} : {:.3} +- {:.3} cm^-2 s^-1\n"
            ss += fmt.format(
                "Integral flux peak (100 MeV - 100 GeV)",
                d["Flux_Peak"].value,
                d["Unc_Flux_Peak"].value,
            )

            # TODO: give time as UTC string, not MET
            ss += "{:<40s} : {:.3} s (Mission elapsed time)\n".format(
                "Time peak", d["Time_Peak"].value
            )
            peak_interval = d["Peak_Interval"].to_value("day")
            ss += "{:<40s} : {:.3} day\n".format("Peak interval", peak_interval)
        else:
            ss += "\nNo peak measured for this source.\n"

        # TODO: Add a lightcurve table with d['Flux_History'] and d['Unc_Flux_History']

        return ss

    def spatial_model(self):
        """Spatial model as a `~gammapy.modeling.models.SpatialModel` object."""
        d = self.data
        ra = d["RAJ2000"]
        dec = d["DEJ2000"]

        if self.is_pointlike:
            model = PointSpatialModel(lon_0=ra, lat_0=dec, frame="icrs")
        else:
            de = self.data_extended
            morph_type = de["Model_Form"].strip()
            e = (1 - (de["Model_SemiMinor"] / de["Model_SemiMajor"]) ** 2.0) ** 0.5
            sigma = de["Model_SemiMajor"]
            phi = de["Model_PosAng"]
            if morph_type == "Disk":
                r_0 = de["Model_SemiMajor"]
                model = DiskSpatialModel(
                    lon_0=ra, lat_0=dec, r_0=r_0, e=e, phi=phi, frame="icrs"
                )
            elif morph_type in ["Map", "Ring", "2D Gaussian x2"]:
                filename = de["Spatial_Filename"].strip() + ".gz"
                if de["version"] < 28:
                    path_extended = "$GAMMAPY_DATA/catalogs/fermi/LAT_extended_sources_8years/Templates/"
                elif de["version"] < 32:
                    path_extended = (
                        "$GAMMAPY_DATA/catalogs/fermi/Extended_12years/Templates/"
                    )
                else:
                    path_extended = (
                        "$GAMMAPY_DATA/catalogs/fermi/Extended_14years/Templates/"
                    )
                path = make_path(path_extended)
                with warnings.catch_warnings():  # ignore FITS units warnings
                    warnings.simplefilter("ignore", FITSFixedWarning)
                    model = TemplateSpatialModel.read(path / filename)
            elif morph_type == "2D Gaussian":
                model = GaussianSpatialModel(
                    lon_0=ra, lat_0=dec, sigma=sigma, e=e, phi=phi, frame="icrs"
                )
            else:
                raise ValueError(f"Invalid spatial model: {morph_type!r}")
        self._set_spatial_errors(model)
        return model

    def spectral_model(self):
        """Best fit spectral model as a `~gammapy.modeling.models.SpectralModel` object."""
        spec_type = self.data["SpectrumType"].strip()

        if spec_type == "PowerLaw":
            tag = "PowerLawSpectralModel"
            pars = {
                "reference": self.data["Pivot_Energy"],
                "amplitude": self.data["PL_Flux_Density"],
                "index": self.data["PL_Index"],
            }
            errs = {
                "amplitude": self.data["Unc_PL_Flux_Density"],
                "index": self.data["Unc_PL_Index"],
            }
        elif spec_type == "LogParabola":
            tag = "LogParabolaSpectralModel"
            pars = {
                "reference": self.data["Pivot_Energy"],
                "amplitude": self.data["LP_Flux_Density"],
                "alpha": self.data["LP_Index"],
                "beta": self.data["LP_beta"],
            }
            errs = {
                "amplitude": self.data["Unc_LP_Flux_Density"],
                "alpha": self.data["Unc_LP_Index"],
                "beta": self.data["Unc_LP_beta"],
            }
        elif spec_type == "PLSuperExpCutoff":
            if "PLEC_ExpfactorS" in self.data:
                tag = "SuperExpCutoffPowerLaw4FGLDR3SpectralModel"
                expfactor = self.data["PLEC_ExpfactorS"]
                expfactor_err = self.data["Unc_PLEC_ExpfactorS"]
                index_1 = self.data["PLEC_IndexS"]
                index_1_err = self.data["Unc_PLEC_IndexS"]
            else:
                tag = "SuperExpCutoffPowerLaw4FGLSpectralModel"
                expfactor = self.data["PLEC_Expfactor"]
                expfactor_err = self.data["Unc_PLEC_Expfactor"]
                index_1 = self.data["PLEC_Index"]
                index_1_err = self.data["Unc_PLEC_Index"]

            pars = {
                "reference": self.data["Pivot_Energy"],
                "amplitude": self.data["PLEC_Flux_Density"],
                "index_1": index_1,
                "index_2": self.data["PLEC_Exp_Index"],
                "expfactor": expfactor,
            }
            errs = {
                "amplitude": self.data["Unc_PLEC_Flux_Density"],
                "index_1": index_1_err,
                "index_2": np.nan_to_num(float(self.data["Unc_PLEC_Exp_Index"])),
                "expfactor": expfactor_err,
            }
        else:
            raise ValueError(f"Invalid spec_type: {spec_type!r}")

        model = Model.create(tag, "spectral", **pars)

        for name, value in errs.items():
            model.parameters[name].error = value

        return model

    @property
    def flux_points_table(self):
        """Flux points as a `~astropy.table.Table`."""
        table = Table()
        table.meta.update(self.flux_points_meta)

        table["e_min"] = self.data["fp_energy_edges"][:-1]
        table["e_max"] = self.data["fp_energy_edges"][1:]

        flux = self._get_flux_values("Flux_Band")
        flux_err = self._get_flux_values("Unc_Flux_Band")
        table["flux"] = flux
        table["flux_errn"] = np.abs(flux_err[:, 0])
        table["flux_errp"] = flux_err[:, 1]

        nuFnu = self._get_flux_values("nuFnu_Band", "erg cm-2 s-1")
        table["e2dnde"] = nuFnu
        table["e2dnde_errn"] = np.abs(nuFnu * flux_err[:, 0] / flux)
        table["e2dnde_errp"] = nuFnu * flux_err[:, 1] / flux

        is_ul = np.isnan(table["flux_errn"])
        table["is_ul"] = is_ul

        # handle upper limits
        table["flux_ul"] = np.nan * flux_err.unit
        flux_ul = compute_flux_points_ul(table["flux"], table["flux_errp"])
        table["flux_ul"][is_ul] = flux_ul[is_ul]

        # handle upper limits
        table["e2dnde_ul"] = np.nan * nuFnu.unit
        e2dnde_ul = compute_flux_points_ul(table["e2dnde"], table["e2dnde_errp"])
        table["e2dnde_ul"][is_ul] = e2dnde_ul[is_ul]

        # Square root of test statistic
        table["sqrt_ts"] = self.data["Sqrt_TS_Band"]
        return table

    def _get_flux_values(self, prefix, unit="cm-2 s-1"):
        values = self.data[prefix]
        return u.Quantity(values, unit)

    def lightcurve(self, interval="1-year"):
        """Lightcurve as a `~gammapy.estimators.FluxPoints` object.

        Parameters
        ----------
        interval : {'1-year', '2-month'}
            Time interval of the lightcurve. Default is '1-year'.
            Note that '2-month' is not available for all catalogue version.
        """
        if interval == "1-year":
            tag = "Flux_History"
            if tag not in self.data or "time_axis" not in self.data:
                raise ValueError(
                    "'1-year' interval is not available for this catalogue version"
                )
            time_axis = self.data["time_axis"]
            tag_sqrt_ts = "Sqrt_TS_History"

        elif interval == "2-month":
            tag = "Flux2_History"
            if tag not in self.data or "time_axis_2" not in self.data:
                raise ValueError(
                    "2-month interval is not available for this catalog version"
                )
            time_axis = self.data["time_axis_2"]
            tag_sqrt_ts = "Sqrt_TS2_History"
        else:
            raise ValueError("Time intervals available are '1-year' or '2-month'")

        energy_axis = MapAxis.from_energy_edges([50, 300000] * u.MeV)
        geom = RegionGeom.create(region=self.position, axes=[energy_axis, time_axis])

        names = ["flux", "flux_errp", "flux_errn", "flux_ul", "ts"]
        maps = Maps.from_geom(geom=geom, names=names)

        maps["flux"].quantity = self.data[tag].reshape(geom.data_shape)
        maps["flux_errp"].quantity = self.data[f"Unc_{tag}"][:, 1].reshape(
            geom.data_shape
        )
        maps["flux_errn"].quantity = -self.data[f"Unc_{tag}"][:, 0].reshape(
            geom.data_shape
        )
        maps["flux_ul"].quantity = compute_flux_points_ul(
            maps["flux"].quantity, maps["flux_errp"].quantity
        ).reshape(geom.data_shape)
        maps["ts"].quantity = (self.data[tag_sqrt_ts] ** 2).reshape(geom.data_shape)

        return FluxPoints.from_maps(
            maps=maps,
            sed_type="flux",
            reference_model=self.sky_model(),
            meta=self.flux_points.meta.copy(),
        )


class SourceCatalogObject3FGL(SourceCatalogObjectFermiBase):
    """One source from the Fermi-LAT 3FGL catalog.

    Catalog is represented by `~gammapy.catalog.SourceCatalog3FGL`.
    """

    _energy_edges = u.Quantity([100, 300, 1000, 3000, 10000, 100000], "MeV")
    _energy_edges_suffix = [
        "100_300",
        "300_1000",
        "1000_3000",
        "3000_10000",
        "10000_100000",
    ]
    energy_range = u.Quantity([100, 100000], "MeV")
    """Energy range used for the catalog.

    Paper says that analysis uses data up to 300 GeV,
    but results are all quoted up to 100 GeV only to
    be consistent with previous catalogs.
    """

    def _info_more(self):
        d = self.data
        ss = "\n*** Other info ***\n\n"
        ss += "{:<20s} : {}\n".format("Other flags", d["Flags"])
        return ss

    def _info_spectral_fit(self):
        d = self.data
        spec_type = d["SpectrumType"].strip()

        ss = "\n*** Spectral info ***\n\n"

        ss += "{:<45s} : {}\n".format("Spectrum type", d["SpectrumType"])
        fmt = "{:<45s} : {:.3f}\n"
        ss += fmt.format("Detection significance (100 MeV - 300 GeV)", d["Signif_Avg"])
        ss += "{:<45s} : {:.1f}\n".format("Significance curvature", d["Signif_Curve"])

        if spec_type == "PowerLaw":
            pass
        elif spec_type == "LogParabola":
            ss += "{:<45s} : {} +- {}\n".format("beta", d["beta"], d["Unc_beta"])
        elif spec_type in ["PLExpCutoff", "PlSuperExpCutoff"]:
            fmt = "{:<45s} : {:.0f} +- {:.0f} {}\n"
            ss += fmt.format(
                "Cutoff energy",
                d["Cutoff"].value,
                d["Unc_Cutoff"].value,
                d["Cutoff"].unit,
            )
        elif spec_type == "PLSuperExpCutoff":
            ss += "{:<45s} : {} +- {}\n".format(
                "Super-exponential cutoff index", d["Exp_Index"], d["Unc_Exp_Index"]
            )
        else:
            raise ValueError(f"Invalid spec_type: {spec_type!r}")

        ss += "{:<45s} : {:.0f} {}\n".format(
            "Pivot energy", d["Pivot_Energy"].value, d["Pivot_Energy"].unit
        )

        ss += "{:<45s} : {:.3f}\n".format(
            "Power law spectral index", d["PowerLaw_Index"]
        )

        fmt = "{:<45s} : {:.3f} +- {:.3f}\n"
        ss += fmt.format("Spectral index", d["Spectral_Index"], d["Unc_Spectral_Index"])

        fmt = "{:<45s} : {:.3} +- {:.3} {}\n"
        ss += fmt.format(
            "Flux Density at pivot energy",
            d["Flux_Density"].value,
            d["Unc_Flux_Density"].value,
            "cm-2 MeV-1 s-1",
        )

        fmt = "{:<45s} : {:.3} +- {:.3} {}\n"
        ss += fmt.format(
            "Integral flux (1 - 100 GeV)",
            d["Flux1000"].value,
            d["Unc_Flux1000"].value,
            "cm-2 s-1",
        )

        fmt = "{:<45s} : {:.3} +- {:.3} {}\n"
        ss += fmt.format(
            "Energy flux (100 MeV - 100 GeV)",
            d["Energy_Flux100"].value,
            d["Unc_Energy_Flux100"].value,
            "erg cm-2 s-1",
        )

        return ss

    def _info_lightcurve(self):
        d = self.data
        ss = "\n*** Lightcurve info ***\n\n"
        ss += "Lightcurve measured in the energy band: 100 MeV - 100 GeV\n\n"

        ss += "{:<15s} : {:.3f}\n".format("Variability index", d["Variability_Index"])

        if np.isfinite(d["Flux_Peak"]):
            ss += "{:<40s} : {:.3f}\n".format(
                "Significance peak (100 MeV - 100 GeV)", d["Signif_Peak"]
            )

            fmt = "{:<40s} : {:.3} +- {:.3} cm^-2 s^-1\n"
            ss += fmt.format(
                "Integral flux peak (100 MeV - 100 GeV)",
                d["Flux_Peak"].value,
                d["Unc_Flux_Peak"].value,
            )

            # TODO: give time as UTC string, not MET
            ss += "{:<40s} : {:.3} s (Mission elapsed time)\n".format(
                "Time peak", d["Time_Peak"].value
            )
            peak_interval = d["Peak_Interval"].to_value("day")
            ss += "{:<40s} : {:.3} day\n".format("Peak interval", peak_interval)
        else:
            ss += "\nNo peak measured for this source.\n"

        # TODO: Add a lightcurve table with d['Flux_History'] and d['Unc_Flux_History']

        return ss

    def spectral_model(self):
        """Best fit spectral model as a `~gammapy.modeling.models.SpectralModel` object."""
        spec_type = self.data["SpectrumType"].strip()

        if spec_type == "PowerLaw":
            tag = "PowerLawSpectralModel"
            pars = {
                "amplitude": self.data["Flux_Density"],
                "reference": self.data["Pivot_Energy"],
                "index": self.data["Spectral_Index"],
            }
            errs = {
                "amplitude": self.data["Unc_Flux_Density"],
                "index": self.data["Unc_Spectral_Index"],
            }
        elif spec_type == "PLExpCutoff":
            tag = "ExpCutoffPowerLaw3FGLSpectralModel"
            pars = {
                "amplitude": self.data["Flux_Density"],
                "reference": self.data["Pivot_Energy"],
                "index": self.data["Spectral_Index"],
                "ecut": self.data["Cutoff"],
            }
            errs = {
                "amplitude": self.data["Unc_Flux_Density"],
                "index": self.data["Unc_Spectral_Index"],
                "ecut": self.data["Unc_Cutoff"],
            }
        elif spec_type == "LogParabola":
            tag = "LogParabolaSpectralModel"
            pars = {
                "amplitude": self.data["Flux_Density"],
                "reference": self.data["Pivot_Energy"],
                "alpha": self.data["Spectral_Index"],
                "beta": self.data["beta"],
            }
            errs = {
                "amplitude": self.data["Unc_Flux_Density"],
                "alpha": self.data["Unc_Spectral_Index"],
                "beta": self.data["Unc_beta"],
            }
        elif spec_type == "PLSuperExpCutoff":
            tag = "SuperExpCutoffPowerLaw3FGLSpectralModel"
            pars = {
                "amplitude": self.data["Flux_Density"],
                "reference": self.data["Pivot_Energy"],
                "index_1": self.data["Spectral_Index"],
                "index_2": self.data["Exp_Index"],
                "ecut": self.data["Cutoff"],
            }
            errs = {
                "amplitude": self.data["Unc_Flux_Density"],
                "index_1": self.data["Unc_Spectral_Index"],
                "index_2": self.data["Unc_Exp_Index"],
                "ecut": self.data["Unc_Cutoff"],
            }
        else:
            raise ValueError(f"Invalid spec_type: {spec_type!r}")

        model = Model.create(tag, "spectral", **pars)

        for name, value in errs.items():
            model.parameters[name].error = value

        return model

    def spatial_model(self):
        """Spatial model as a `~gammapy.modeling.models.SpatialModel` object."""
        d = self.data
        ra = d["RAJ2000"]
        dec = d["DEJ2000"]

        if self.is_pointlike:
            model = PointSpatialModel(lon_0=ra, lat_0=dec, frame="icrs")
        else:
            de = self.data_extended
            morph_type = de["Model_Form"].strip()
            e = (1 - (de["Model_SemiMinor"] / de["Model_SemiMajor"]) ** 2.0) ** 0.5
            sigma = de["Model_SemiMajor"]
            phi = de["Model_PosAng"]
            if morph_type == "Disk":
                r_0 = de["Model_SemiMajor"]
                model = DiskSpatialModel(
                    lon_0=ra, lat_0=dec, r_0=r_0, e=e, phi=phi, frame="icrs"
                )
            elif morph_type in ["Map", "Ring", "2D Gaussian x2"]:
                filename = de["Spatial_Filename"].strip()
                path = make_path(
                    "$GAMMAPY_DATA/catalogs/fermi/Extended_archive_v15/Templates/"
                )
                model = TemplateSpatialModel.read(path / filename)
            elif morph_type == "2D Gaussian":
                model = GaussianSpatialModel(
                    lon_0=ra, lat_0=dec, sigma=sigma, e=e, phi=phi, frame="icrs"
                )
            else:
                raise ValueError(f"Invalid spatial model: {morph_type!r}")
        self._set_spatial_errors(model)
        return model

    @property
    def flux_points_table(self):
        """Flux points as a `~astropy.table.Table`."""
        table = Table()
        table.meta.update(self.flux_points_meta)

        table["e_min"] = self._energy_edges[:-1]
        table["e_max"] = self._energy_edges[1:]

        flux = self._get_flux_values("Flux")
        flux_err = self._get_flux_values("Unc_Flux")
        table["flux"] = flux
        table["flux_errn"] = np.abs(flux_err[:, 0])
        table["flux_errp"] = flux_err[:, 1]

        nuFnu = self._get_flux_values("nuFnu", "erg cm-2 s-1")
        table["e2dnde"] = nuFnu
        table["e2dnde_errn"] = np.abs(nuFnu * flux_err[:, 0] / flux)
        table["e2dnde_errp"] = nuFnu * flux_err[:, 1] / flux

        is_ul = np.isnan(table["flux_errn"])
        table["is_ul"] = is_ul

        # handle upper limits
        table["flux_ul"] = np.nan * flux_err.unit
        flux_ul = compute_flux_points_ul(table["flux"], table["flux_errp"])
        table["flux_ul"][is_ul] = flux_ul[is_ul]

        # handle upper limits
        table["e2dnde_ul"] = np.nan * nuFnu.unit
        e2dnde_ul = compute_flux_points_ul(table["e2dnde"], table["e2dnde_errp"])
        table["e2dnde_ul"][is_ul] = e2dnde_ul[is_ul]

        # Square root of test statistic
        table["sqrt_ts"] = [self.data["Sqrt_TS" + _] for _ in self._energy_edges_suffix]
        return table

    def _get_flux_values(self, prefix, unit="cm-2 s-1"):
        values = [self.data[prefix + _] for _ in self._energy_edges_suffix]
        return u.Quantity(values, unit)

    def lightcurve(self):
        """Lightcurve as a `~gammapy.estimators.FluxPoints` object."""
        time_axis = self.data["time_axis"]
        tag = "Flux_History"

        energy_axis = MapAxis.from_energy_edges(self.energy_range)
        geom = RegionGeom.create(region=self.position, axes=[energy_axis, time_axis])

        names = ["flux", "flux_errp", "flux_errn", "flux_ul"]
        maps = Maps.from_geom(geom=geom, names=names)

        maps["flux"].quantity = self.data[tag].reshape(geom.data_shape)
        maps["flux_errp"].quantity = self.data[f"Unc_{tag}"][:, 1].reshape(
            geom.data_shape
        )
        maps["flux_errn"].quantity = -self.data[f"Unc_{tag}"][:, 0].reshape(
            geom.data_shape
        )
        maps["flux_ul"].quantity = compute_flux_points_ul(
            maps["flux"].quantity, maps["flux_errp"].quantity
        ).reshape(geom.data_shape)
        is_ul = np.isnan(maps["flux_errn"])
        maps["flux_ul"].data[~is_ul] = np.nan

        return FluxPoints.from_maps(
            maps=maps,
            sed_type="flux",
            reference_model=self.sky_model(),
            meta=self.flux_points_meta.copy(),
        )


class SourceCatalogObject2FHL(SourceCatalogObjectFermiBase):
    """One source from the Fermi-LAT 2FHL catalog.

    Catalog is represented by `~gammapy.catalog.SourceCatalog2FHL`.
    """

    asso = ["ASSOC", "3FGL_Name", "1FHL_Name", "TeVCat_Name"]
    _energy_edges = u.Quantity([50, 171, 585, 2000], "GeV")
    _energy_edges_suffix = ["50_171", "171_585", "585_2000"]
    energy_range = u.Quantity([0.05, 2], "TeV")
    """Energy range used for the catalog."""

    def _info_more(self):
        d = self.data
        ss = "\n*** Other info ***\n\n"
        fmt = "{:<32s} : {:.3f}\n"
        ss += fmt.format("Test statistic (50 GeV - 2 TeV)", d["TS"])
        return ss

    def _info_position(self):
        d = self.data
        ss = "\n*** Position info ***\n\n"
        ss += "{:<20s} : {:.3f}\n".format("RA", d["RAJ2000"])
        ss += "{:<20s} : {:.3f}\n".format("DEC", d["DEJ2000"])
        ss += "{:<20s} : {:.3f}\n".format("GLON", d["GLON"])
        ss += "{:<20s} : {:.3f}\n".format("GLAT", d["GLAT"])

        ss += "\n"
        ss += "{:<20s} : {:.4f}\n".format("Error on position (68%)", d["Pos_err_68"])
        ss += "{:<20s} : {:.0f}\n".format("ROI number", d["ROI"])
        return ss

    def _info_spectral_fit(self):
        d = self.data

        ss = "\n*** Spectral fit info ***\n\n"

        fmt = "{:<32s} : {:.3f} +- {:.3f}\n"
        ss += fmt.format(
            "Power-law spectral index", d["Spectral_Index"], d["Unc_Spectral_Index"]
        )

        ss += "{:<32s} : {:.3} +- {:.3} {}\n".format(
            "Integral flux (50 GeV - 2 TeV)",
            d["Flux50"].value,
            d["Unc_Flux50"].value,
            "cm-2 s-1",
        )

        ss += "{:<32s} : {:.3} +- {:.3} {}\n".format(
            "Energy flux (50 GeV - 2 TeV)",
            d["Energy_Flux50"].value,
            d["Unc_Energy_Flux50"].value,
            "erg cm-2 s-1",
        )

        return ss

    @property
    def is_pointlike(self):
        return self.data["Source_Name"].strip()[-1] != "e"

    def spatial_model(self):
        """Spatial model as a `~gammapy.modeling.models.SpatialModel` object."""
        d = self.data
        ra = d["RAJ2000"]
        dec = d["DEJ2000"]

        if self.is_pointlike:
            model = PointSpatialModel(lon_0=ra, lat_0=dec, frame="icrs")
        else:
            de = self.data_extended
            morph_type = de["Model_Form"].strip()
            e = (1 - (de["Model_SemiMinor"] / de["Model_SemiMajor"]) ** 2.0) ** 0.5
            sigma = de["Model_SemiMajor"]
            phi = de["Model_PosAng"]
            if morph_type in ["Disk", "Elliptical Disk"]:
                r_0 = de["Model_SemiMajor"]
                model = DiskSpatialModel(
                    lon_0=ra, lat_0=dec, r_0=r_0, e=e, phi=phi, frame="icrs"
                )
            elif morph_type in ["Map", "Ring", "2D Gaussian x2"]:
                filename = de["Spatial_Filename"].strip()
                path = make_path(
                    "$GAMMAPY_DATA/catalogs/fermi/Extended_archive_v15/Templates/"
                )
                return TemplateSpatialModel.read(path / filename)
            elif morph_type in ["2D Gaussian", "Elliptical 2D Gaussian"]:
                model = GaussianSpatialModel(
                    lon_0=ra, lat_0=dec, sigma=sigma, e=e, phi=phi, frame="icrs"
                )
            else:
                raise ValueError(f"Invalid spatial model: {morph_type!r}")

        self._set_spatial_errors(model)
        return model

    def spectral_model(self):
        """Best fit spectral model as a `~gammapy.modeling.models.SpectralModel`."""
        tag = "PowerLaw2SpectralModel"
        pars = {
            "amplitude": self.data["Flux50"],
            "emin": self.energy_range[0],
            "emax": self.energy_range[1],
            "index": self.data["Spectral_Index"],
        }
        errs = {
            "amplitude": self.data["Unc_Flux50"],
            "index": self.data["Unc_Spectral_Index"],
        }

        model = Model.create(tag, "spectral", **pars)

        for name, value in errs.items():
            model.parameters[name].error = value

        return model

    @property
    def flux_points_table(self):
        """Flux points as a `~astropy.table.Table`."""
        table = Table()
        table.meta.update(self.flux_points_meta)
        table["e_min"] = self._energy_edges[:-1]
        table["e_max"] = self._energy_edges[1:]
        table["flux"] = self._get_flux_values("Flux")
        flux_err = self._get_flux_values("Unc_Flux")
        table["flux_errn"] = np.abs(flux_err[:, 0])
        table["flux_errp"] = flux_err[:, 1]

        # handle upper limits
        is_ul = np.isnan(table["flux_errn"])
        table["is_ul"] = is_ul
        table["flux_ul"] = np.nan * flux_err.unit
        flux_ul = compute_flux_points_ul(table["flux"], table["flux_errp"])
        table["flux_ul"][is_ul] = flux_ul[is_ul]
        return table

    def _get_flux_values(self, prefix, unit="cm-2 s-1"):
        values = [self.data[prefix + _ + "GeV"] for _ in self._energy_edges_suffix]
        return u.Quantity(values, unit)


class SourceCatalogObject3FHL(SourceCatalogObjectFermiBase):
    """One source from the Fermi-LAT 3FHL catalog.

    Catalog is represented by `~gammapy.catalog.SourceCatalog3FHL`.
    """

    asso = ["ASSOC1", "ASSOC2", "ASSOC_TEV", "ASSOC_GAM"]
    energy_range = u.Quantity([0.01, 2], "TeV")
    """Energy range used for the catalog."""

    _energy_edges = u.Quantity([10, 20, 50, 150, 500, 2000], "GeV")

    def _info_position(self):
        d = self.data
        ss = "\n*** Position info ***\n\n"
        ss += "{:<20s} : {:.3f}\n".format("RA", d["RAJ2000"])
        ss += "{:<20s} : {:.3f}\n".format("DEC", d["DEJ2000"])
        ss += "{:<20s} : {:.3f}\n".format("GLON", d["GLON"])
        ss += "{:<20s} : {:.3f}\n".format("GLAT", d["GLAT"])

        # TODO: All sources are non-elliptical; just give one number for radius?
        ss += "\n"
        ss += "{:<20s} : {:.4f}\n".format("Semimajor (95%)", d["Conf_95_SemiMajor"])
        ss += "{:<20s} : {:.4f}\n".format("Semiminor (95%)", d["Conf_95_SemiMinor"])
        ss += "{:<20s} : {:.2f}\n".format("Position angle (95%)", d["Conf_95_PosAng"])
        ss += "{:<20s} : {:.0f}\n".format("ROI number", d["ROI_num"])

        return ss

    def _info_spectral_fit(self):
        d = self.data
        spec_type = d["SpectrumType"].strip()

        ss = "\n*** Spectral fit info ***\n\n"

        ss += "{:<32s} : {}\n".format("Spectrum type", d["SpectrumType"])
        ss += "{:<32s} : {:.1f}\n".format("Significance curvature", d["Signif_Curve"])

        # Power-law parameters are always given; give in any case
        fmt = "{:<32s} : {:.3f} +- {:.3f}\n"
        ss += fmt.format(
            "Power-law spectral index", d["PowerLaw_Index"], d["Unc_PowerLaw_Index"]
        )

        if spec_type == "PowerLaw":
            pass
        elif spec_type == "LogParabola":
            fmt = "{:<32s} : {:.3f} +- {:.3f}\n"
            ss += fmt.format(
                "LogParabolaSpectralModel spectral index",
                d["Spectral_Index"],
                d["Unc_Spectral_Index"],
            )

            ss += "{:<32s} : {:.3f} +- {:.3f}\n".format(
                "LogParabolaSpectralModel beta", d["beta"], d["Unc_beta"]
            )
        else:
            raise ValueError(f"Invalid spec_type: {spec_type!r}")

        ss += "{:<32s} : {:.1f} {}\n".format(
            "Pivot energy", d["Pivot_Energy"].value, d["Pivot_Energy"].unit
        )

        ss += "{:<32s} : {:.3} +- {:.3} {}\n".format(
            "Flux Density at pivot energy",
            d["Flux_Density"].value,
            d["Unc_Flux_Density"].value,
            "cm-2 GeV-1 s-1",
        )

        ss += "{:<32s} : {:.3} +- {:.3} {}\n".format(
            "Integral flux (10 GeV - 1 TeV)",
            d["Flux"].value,
            d["Unc_Flux"].value,
            "cm-2 s-1",
        )

        ss += "{:<32s} : {:.3} +- {:.3} {}\n".format(
            "Energy flux (10 GeV - TeV)",
            d["Energy_Flux"].value,
            d["Unc_Energy_Flux"].value,
            "erg cm-2 s-1",
        )

        return ss

    def _info_more(self):
        d = self.data
        ss = "\n*** Other info ***\n\n"

        fmt = "{:<32s} : {:.3f}\n"
        ss += fmt.format("Significance (10 GeV - 2 TeV)", d["Signif_Avg"])
        ss += "{:<32s} : {:.1f}\n".format("Npred", d["Npred"])

        ss += "\n{:<16s} : {:.3f} {}\n".format(
            "HEP Energy", d["HEP_Energy"].value, d["HEP_Energy"].unit
        )
        ss += "{:<16s} : {:.3f}\n".format("HEP Probability", d["HEP_Prob"])

        ss += "{:<16s} : {}\n".format("Bayesian Blocks", d["Variability_BayesBlocks"])

        ss += "{:<16s} : {:.3f}\n".format("Redshift", d["Redshift"])
        ss += "{:<16s} : {:.3} {}\n".format(
            "NuPeak_obs", d["NuPeak_obs"].value, d["NuPeak_obs"].unit
        )

        return ss

    def spectral_model(self):
        """Best fit spectral model as a `~gammapy.modeling.models.SpectralModel` object."""
        d = self.data
        spec_type = self.data["SpectrumType"].strip()

        if spec_type == "PowerLaw":
            tag = "PowerLawSpectralModel"
            pars = {
                "reference": d["Pivot_Energy"],
                "amplitude": d["Flux_Density"],
                "index": d["PowerLaw_Index"],
            }
            errs = {
                "amplitude": d["Unc_Flux_Density"],
                "index": d["Unc_PowerLaw_Index"],
            }
        elif spec_type == "LogParabola":
            tag = "LogParabolaSpectralModel"
            pars = {
                "reference": d["Pivot_Energy"],
                "amplitude": d["Flux_Density"],
                "alpha": d["Spectral_Index"],
                "beta": d["beta"],
            }
            errs = {
                "amplitude": d["Unc_Flux_Density"],
                "alpha": d["Unc_Spectral_Index"],
                "beta": d["Unc_beta"],
            }
        else:
            raise ValueError(f"Invalid spec_type: {spec_type!r}")

        model = Model.create(tag, "spectral", **pars)

        for name, value in errs.items():
            model.parameters[name].error = value

        return model

    @property
    def flux_points_table(self):
        """Flux points as a `~astropy.table.Table`."""
        table = Table()
        table.meta.update(self.flux_points_meta)
        table["e_min"] = self._energy_edges[:-1]
        table["e_max"] = self._energy_edges[1:]

        flux = self.data["Flux_Band"]
        flux_err = self.data["Unc_Flux_Band"]
        e2dnde = self.data["nuFnu"]

        table["flux"] = flux
        table["flux_errn"] = np.abs(flux_err[:, 0])
        table["flux_errp"] = flux_err[:, 1]

        table["e2dnde"] = e2dnde
        table["e2dnde_errn"] = np.abs(e2dnde * flux_err[:, 0] / flux)
        table["e2dnde_errp"] = e2dnde * flux_err[:, 1] / flux

        is_ul = np.isnan(table["flux_errn"])
        table["is_ul"] = is_ul

        # handle upper limits
        table["flux_ul"] = np.nan * flux_err.unit
        flux_ul = compute_flux_points_ul(table["flux"], table["flux_errp"])
        table["flux_ul"][is_ul] = flux_ul[is_ul]

        table["e2dnde_ul"] = np.nan * e2dnde.unit
        e2dnde_ul = compute_flux_points_ul(table["e2dnde"], table["e2dnde_errp"])
        table["e2dnde_ul"][is_ul] = e2dnde_ul[is_ul]

        # Square root of test statistic
        table["sqrt_ts"] = self.data["Sqrt_TS_Band"]
        return table

    def spatial_model(self):
        """Source spatial model as a `~gammapy.modeling.models.SpatialModel` object."""
        d = self.data
        ra = d["RAJ2000"]
        dec = d["DEJ2000"]

        if self.is_pointlike:
            model = PointSpatialModel(lon_0=ra, lat_0=dec, frame="icrs")
        else:
            de = self.data_extended
            morph_type = de["Spatial_Function"].strip()
            e = (1 - (de["Model_SemiMinor"] / de["Model_SemiMajor"]) ** 2.0) ** 0.5
            sigma = de["Model_SemiMajor"]
            phi = de["Model_PosAng"]
            if morph_type == "RadialDisk":
                r_0 = de["Model_SemiMajor"]
                model = DiskSpatialModel(
                    lon_0=ra, lat_0=dec, r_0=r_0, e=e, phi=phi, frame="icrs"
                )
            elif morph_type in ["SpatialMap"]:
                filename = de["Spatial_Filename"].strip()
                path = make_path(
                    "$GAMMAPY_DATA/catalogs/fermi/Extended_archive_v18/Templates/"
                )
                model = TemplateSpatialModel.read(path / filename)
            elif morph_type == "RadialGauss":
                model = GaussianSpatialModel(
                    lon_0=ra, lat_0=dec, sigma=sigma, e=e, phi=phi, frame="icrs"
                )
            else:
                raise ValueError(f"Invalid morph_type: {morph_type!r}")
        self._set_spatial_errors(model)
        return model


class SourceCatalogObject2PC(SourceCatalogObjectFermiPCBase):
    """One source from the 2PC catalog."""

    @property
    def _auxiliary_filename(self):
        return make_path(
            f"$GAMMAPY_DATA/catalogs/fermi/2PC_auxiliary/PSR{self.name}_2PC_data.fits.gz"
        )

    def _info_more(self):
        d = self.data
        ss = "\n*** Other info ***\n\n"
        ss += "{:<20s} : {:s}\n".format("Binary", d["Binary"])
        return ss

    def _info_pulsar(self):
        d = self.data
        ss = "\n*** Pulsar info ***\n\n"
        ss += "{:<20s} : {:.3f}\n".format("Period", d["Period"])
        ss += "{:<20s} : {:.3e}\n".format("P_Dot", d["P_Dot"])
        ss += "{:<20s} : {:.3e}\n".format("E_Dot", d["E_Dot"])
        ss += "{:<20s} : {}\n".format("Type", d["Type"])
        return ss

    def _info_spectral_fit(self):
        d = self.data_spectral
        ss = "\n*** Spectral info ***\n\n"
        if d is None:
            ss += "No spectral info available.\n"
            return ss
        ss += "{:<20s} : {}\n".format("On peak", d["On_Peak"])
        ss += "{:<20s} : {:.0f}\n".format("TS DC", d["TS_DC"])
        ss += "{:<20s} : {:.0f}\n".format("TS cutoff", d["TS_Cutoff"])
        ss += "{:<20s} : {:.0f}\n".format("TS b free", d["TS_bfree"])

        indentation = " " * 4
        fmt_e = "{}{:<20s} : {:.3e} +- {:.3e}\n"
        fmt_f = "{}{:<20s} : {:.3f} +- {:.3f}\n"

        if not isinstance(d["PLEC1_Prefactor"], np.ma.core.MaskedConstant):
            ss += "\n{}* PLSuperExpCutoff b = 1 *\n\n".format(indentation)
            ss += fmt_e.format(
                indentation, "Amplitude", d["PLEC1_Prefactor"], d["Unc_PLEC1_Prefactor"]
            )
            ss += fmt_f.format(
                indentation,
                "Index 1",
                d["PLEC1_Photon_Index"],
                d["Unc_PLEC1_Photon_Index"],
            )
            ss += "{}{:<20s} : {:.3f}\n".format(indentation, "Index 2", 1)
            ss += "{}{:<20s} : {:.3f}\n".format(
                indentation, "Reference", d["PLEC1_Scale"]
            )
            ss += fmt_f.format(
                indentation, "Ecut", d["PLEC1_Cutoff"], d["Unc_PLEC1_Cutoff"]
            )

        if not isinstance(d["PLEC_Prefactor"], np.ma.core.MaskedConstant):
            ss += "\n{}* PLSuperExpCutoff b free *\n\n".format(indentation)
            ss += fmt_e.format(
                indentation, "Amplitude", d["PLEC_Prefactor"], d["Unc_PLEC_Prefactor"]
            )
            ss += fmt_f.format(
                indentation,
                "Index 1",
                d["PLEC_Photon_Index"],
                d["Unc_PLEC_Photon_Index"],
            )
            ss += fmt_f.format(
                indentation,
                "Index 2",
                d["PLEC_Exponential_Index"],
                d["Unc_PLEC_Exponential_Index"],
            )

            ss += "{}{:<20s} : {:.3f}\n".format(
                indentation, "Reference", d["PLEC_Scale"]
            )
            ss += fmt_f.format(
                indentation, "Ecut", d["PLEC_Cutoff"], d["Unc_PLEC_Cutoff"]
            )

        if not isinstance(d["PL_Prefactor"], np.ma.core.MaskedConstant):
            ss += "\n{}* PowerLaw *\n\n".format(indentation)
            ss += fmt_e.format(
                indentation, "Amplitude", d["PL_Prefactor"], d["Unc_PL_Prefactor"]
            )
            ss += fmt_f.format(
                indentation, "Index", d["PL_Photon_Index"], d["Unc_PL_Photon_Index"]
            )
            ss += "{}{:<20s} : {:.3f}\n".format(indentation, "Reference", d["PL_Scale"])

        return ss

    def _info_phasogram(self):
        d = self.data
        ss = "\n*** Phasogram info ***\n\n"
        ss += "{:<20s} : {:d}\n".format("Number of peaks", d["Num_Peaks"])
        ss += "{:<20s} : {:.3f}\n".format("Peak separation", d["Peak_Sep"])
        return ss

    def spectral_model(self):
        d = self.data_spectral
        if d is None:
            log.warning(f"No spectral model available for source {self.name}")
            return None
        if d["TS_Cutoff"] < 9:
            tag = "PowerLawSpectralModel"
            pars = {
                "reference": d["PL_Scale"],
                "amplitude": d["PL_Prefactor"],
                "index": d["PL_Photon_Index"],
            }
            errs = {
                "amplitude": d["Unc_PL_Prefactor"],
                "index": d["Unc_PL_Photon_Index"],
            }
        elif d["TS_bfree"] >= 9:
            tag = "SuperExpCutoffPowerLaw3FGLSpectralModel"
            pars = {
                "index_1": d["PLEC_Photon_Index"],
                "index_2": d["PLEC_Exponential_Index"],
                "amplitude": d["PLEC_Prefactor"],
                "reference": d["PLEC_Scale"],
                "ecut": d["PLEC_Cutoff"],
            }
            errs = {
                "index_1": d["Unc_PLEC_Photon_Index"],
                "index_2": d["Unc_PLEC_Exponential_Index"],
                "amplitude": d["Unc_PLEC_Prefactor"],
                "ecut": d["Unc_PLEC_Cutoff"],
            }
        elif d["TS_bfree"] < 9:
            tag = "SuperExpCutoffPowerLaw3FGLSpectralModel"
            pars = {
                "index_1": d["PLEC1_Photon_Index"],
                "index_2": 1,
                "amplitude": d["PLEC1_Prefactor"],
                "reference": d["PLEC1_Scale"],
                "ecut": d["PLEC1_Cutoff"],
            }
            errs = {
                "index_1": d["Unc_PLEC1_Photon_Index"],
                "amplitude": d["Unc_PLEC1_Prefactor"],
                "ecut": d["Unc_PLEC1_Cutoff"],
            }
        else:
            log.warning(f"No spectral model available for source {self.name}")
            return None

        model = Model.create(tag, "spectral", **pars)

        for name, value in errs.items():
            model.parameters[name].error = value

        return model

    @property
    def flux_points_table(self):
        """Flux points (`~astropy.table.Table`)."""

        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", u.UnitsWarning)
                fp_data = Table.read(self._auxiliary_filename, hdu="PULSAR_SED")
        except (KeyError, FileNotFoundError):
            log.warning(f"No flux points available for source {self.name}")
            return None
        table = Table()

        table["e_min"] = fp_data["Energy_Min"]
        table["e_max"] = fp_data["Energy_Max"]
        table["e_ref"] = fp_data["Center_Energy"]

        table["flux"] = fp_data["PhotonFlux"]
        table["flux_err"] = fp_data["Unc_PhotonFlux"]

        table["e2dnde"] = fp_data["EnergyFlux"]
        table["e2dnde_err"] = fp_data["Unc_EnergyFlux"]

        is_ul = np.where(table["e2dnde_err"] == 0, True, False)
        table["is_ul"] = is_ul

        table["flux_ul"] = np.nan * table["flux_err"].unit
        flux_ul = compute_flux_points_ul(table["flux"], table["flux_err"])
        table["flux_ul"][is_ul] = flux_ul[is_ul]

        table["e2dnde_ul"] = np.nan * table["e2dnde"].unit
        e2dnde_ul = compute_flux_points_ul(table["e2dnde"], table["e2dnde_err"])
        table["e2dnde_ul"][is_ul] = e2dnde_ul[is_ul]

        return table


class SourceCatalogObject3PC(SourceCatalogObjectFermiPCBase):
    """One source from the 3PC catalog."""

    asso = ["assoc_new"]

    _energy_edges = u.Quantity([50, 100, 300, 1_000, 3e3, 1e4, 3e4, 1e5, 1e6], "MeV")

    _pulse_profile_column_name = [
        "GT100_WtCnt",
        "50_100_WtCt",
        "100_300_WtCt",
        "300_1000_WtCt",
        "1000_3000_WtCt",
        "3000_100000_WtCt",
        "10000_100000_WtCt",
    ]

    @property
    def _auxiliary_filename(self):
        return make_path(
            f"$GAMMAPY_DATA/catalogs/fermi/3PC_auxiliary_20230728/{self.name}_3PC_data.fits.gz"
        )

    def _info_pulsar(self):
        d = self.data
        ss = "\n*** Pulsar info ***\n\n"
        ss += "{:<20s} : {:.3f}\n".format("Period", d["P0"])
        ss += "{:<20s} : {:.3e}\n".format("P_Dot", d["P1"])
        ss += "{:<20s} : {:.3e}\n".format("E_Dot", d["EDOT"])
        return ss

    def _info_phasogram(self):
        d = self.data
        ss = "\n*** Phasogram info ***\n\n"
        if not isinstance(d["NPEAK"], np.ma.core.MaskedConstant):
            npeak = d["NPEAK"]
            ss += "{:<20s} : {:.3f}\n".format("Number of peaks", npeak)
            if npeak > 1:
                ss += "{:<20s} : {:.3f}\n".format("Ph1 (peak one)", d["PHI1"])
                ss += "{:<20s} : {:.3f}\n".format(
                    "Ph2 (peak two)", d["PHI1"] + d["PKSEP"]
                )
                ss += "{:<20s} : {:.3f}\n".format("Peak separation", d["PKSEP"])
            else:
                if not isinstance(d["PHI1"], np.ma.core.MaskedConstant):
                    ss += "{:<20s} : {:.3f}\n".format("Ph1 (peak one)", d["PHI1"])
        else:
            ss += "No phasogram info available.\n"
        return ss

    def _info_spectral_fit(self):
        d = self.data_spectral
        ss = "\n*** Spectral info ***\n\n"
        if d is None:
            ss += "No spectral info available.\n"
            return ss
        ss += "{:<20s} : {:.0f}\n".format("TS", d["Test_Statistic"])
        ss += "{:<20s} : {:.0f}\n".format("Significance (DC)", d["Signif_Avg"])
        ss += "{:<20s} : {:s}\n".format("Spectrum Type", d["SpectrumType"])

        indentation = " " * 4
        fmt_e = "{}{:<20s} : {:.3e} +- {:.3e}\n"
        fmt_f = "{}{:<20s} : {:.3f} +- {:.3f}\n"

        if not isinstance(d["PLEC_Flux_Density_b23"], np.ma.core.MaskedConstant):
            ss += "\n{}* SuperExpCutoffPowerLaw4FGLDR3 b = 2/3 *\n\n".format(
                indentation
            )
            ss += fmt_e.format(
                indentation,
                "Amplitude",
                d["PLEC_Flux_Density_b23"],
                d["Unc_PLEC_Flux_Density_b23"],
            )
            ss += fmt_f.format(
                indentation,
                "Index 1",
                -d["PLEC_IndexS_b23"],
                d["Unc_PLEC_IndexS_b23"],
            )
            ss += "{}{:<20s} : {:.3f}\n".format(indentation, "Index 2", 0.6667)
            ss += "{}{:<20s} : {:.3f}\n".format(
                indentation, "Reference", d["Pivot_Energy_b23"]
            )
            ss += fmt_f.format(
                indentation,
                "Expfactor",
                d["PLEC_ExpfactorS_b23"],
                d["Unc_PLEC_ExpfactorS_b23"],
            )

        if not isinstance(d["PLEC_Flux_Density_bfr"], np.ma.core.MaskedConstant):
            ss += "\n{}* SuperExpCutoffPowerLaw4FGLDR3 b free *\n\n".format(indentation)
            ss += fmt_e.format(
                indentation,
                "Amplitude",
                d["PLEC_Flux_Density_bfr"],
                d["Unc_PLEC_Flux_Density_bfr"],
            )
            ss += fmt_f.format(
                indentation,
                "Index 1",
                -d["PLEC_IndexS_bfr"],
                d["Unc_PLEC_IndexS_bfr"],
            )
            ss += fmt_f.format(
                indentation,
                "Index 2",
                d["PLEC_Exp_Index_bfr"],
                d["Unc_PLEC_Exp_Index_bfr"],
            )
            ss += "{}{:<20s} : {:.3f}\n".format(
                indentation, "Reference", d["Pivot_Energy_bfr"]
            )
            ss += fmt_f.format(
                indentation,
                "Expfactor",
                d["PLEC_ExpfactorS_bfr"],
                d["Unc_PLEC_ExpfactorS_bfr"],
            )
        return ss

    @property
    def pulse_profile_best_fit(self):
        """
        Best fit of the > 100 MeV 3PC pulse profile.

        Returns
        -------
        best_fit_profile: `~gammapy.maps.RegionNDMap`
            Map containing the best fit.
        """
        table = Table.read(self._auxiliary_filename, hdu="BEST_FIT_LC")

        # For best-fit profile, Ph_min and Ph_max are equal and represent bin centers.
        phases = MapAxis.from_nodes(table["Ph_Min"], name="phase", interp="lin")
        best_fit_profile = RegionNDMap.create(
            region=None, axes=[phases], data=table["Intensity"]
        )
        return best_fit_profile

    @property
    def pulse_profile_radio(self):
        """
        Radio pulse profile provided in the auxiliary file of 3PC.

        Returns
        -------

        radio_profile: `~gammapy.maps.RegionNDMap`
            Map containing the radio profile.
        """
        table = Table.read(self._auxiliary_filename, hdu="RADIO_PROFILE")

        # Need to do this because some PSR (J0540-6919) has duplicates
        ph_node, unique_idx = np.unique(table["Ph_Min"], return_index=True)
        data = table["Norm_Intensity"][unique_idx]

        # For radio pulse profile, Ph_min and Ph_max are equal and represent bin centers.
        phases = MapAxis.from_nodes(ph_node, name="phase", interp="lin")
        radio_profile = RegionNDMap.create(region=None, axes=[phases], data=data)
        return radio_profile

    @property
    def pulse_profiles(self):
        """
        The 3PC pulse profiles are provided in different energy ranges, each represented in weighted counts.
        These profiles are stored in a `~gammapy.maps.Maps` of `~gammapy.maps.RegionNDMap`, one per energy bin.

        The `~gammapy.maps.Maps` keys correspond to specific energy ranges as follows:

        - `GT100_WtCnt`: > 0.1 GeV
        - `50_100_WtCt`: 0.05 – 0.1 GeV
        - `100_300_WtCt`: 0.1 – 0.3 GeV
        - `300_1000_WtCt`: 0.3 – 1 GeV
        - `1000_3000_WtCt`: 1 – 3 GeV
        - `3000_100000_WtCt`: 3 – 1000 GeV
        - `10000_100000_WtCt`: 10 – 1000 GeV

        Each pulse profile has an associated uncertainty map, which can be accessed by
        prepending `"Unc_"` to the corresponding key.

        Returns
        -------
        maps: `~gammapy.maps.Maps`
            Maps containing the pulse profile in the different energy bin.
        """

        table = Table.read(self._auxiliary_filename, hdu="GAMMA_LC")
        phases = MapAxis.from_edges(
            np.unique(np.concatenate([table["Ph_Min"], table["Ph_Max"]])),
            name="phase",
            interp="lin",
        )
        geom = RegionGeom(region=None, axes=[phases])
        names = np.concatenate(
            [
                self._pulse_profile_column_name,
                [f"Unc_{name}" for name in self._pulse_profile_column_name],
            ]
        )
        maps = Maps.from_geom(
            geom=geom,
            names=names,
            kwargs_list=[{"data": table[name].data} for name in names],
        )
        return maps

    def spectral_model(self, fit="auto"):
        """
        In the 3PC, Fermi-LAT collaboration tried to fit a
        `~gammapy.modelling.models.SuperExpCutoffPowerLaw4FGLDR3SpectralModel` with the
        exponential index `index_2` free, or fixed to 2/3. These two models are referred
        as "b free" and "b 23". For most pulsars, both models are available. However,
        in some cases the "b free" model did not fit correctly.

        Parameters
        ----------

        fit : str, optional
            Which fitted model to return. The user can choose between "auto", "b free"
            and "b 23". "auto" will always try to return the "b free" first and fall
            back to the "b 23" fit if "b free" is not available. Default is "auto".
        """
        d = self.data_spectral
        if d is None or d["SpectrumType"] != "PLSuperExpCutoff4":
            log.warning(f"No spectral model available for source {self.name}")
            return None

        tag = "SuperExpCutoffPowerLaw4FGLDR3SpectralModel"
        if not (
            isinstance(d["PLEC_IndexS_bfr"], np.ma.core.masked_array) or (fit == "b 23")
        ):
            pars = {
                "reference": d["Pivot_Energy_bfr"],
                "amplitude": d["PLEC_Flux_Density_bfr"],
                "index_1": -d["PLEC_IndexS_bfr"],
                "index_2": d["PLEC_Exp_Index_bfr"],
                "expfactor": d["PLEC_ExpfactorS_bfr"],
            }
            errs = {
                "amplitude": d["Unc_PLEC_Flux_Density_bfr"],
                "index_1": d["Unc_PLEC_IndexS_bfr"],
                "index_2": d["Unc_PLEC_Exp_Index_bfr"],
                "expfactor": d["Unc_PLEC_ExpfactorS_bfr"],
            }
        else:
            pars = {
                "reference": d["Pivot_Energy_b23"],
                "amplitude": d["PLEC_Flux_Density_b23"],
                "index_1": -d["PLEC_IndexS_b23"],
                "index_2": d["PLEC_Exp_Index_b23"],
                "expfactor": d["PLEC_ExpfactorS_b23"],
            }
            errs = {
                "amplitude": d["Unc_PLEC_Flux_Density_b23"],
                "index_1": d["Unc_PLEC_IndexS_b23"],
                "expfactor": d["Unc_PLEC_ExpfactorS_b23"],
            }

        model = Model.create(tag, "spectral", **pars)

        for name, value in errs.items():
            model.parameters[name].error = value

        return model

    @property
    def flux_points_table(self):
        """Flux points (`~astropy.table.Table`). Flux point is an upper limit if
        its significance is less than 2."""
        fp_data = self.data_spectral
        if fp_data is None:
            log.warning(f"No flux points available for source {self.name}")
            return None
        table = Table()

        table["e_min"] = self._energy_edges[:-1]
        table["e_max"] = self._energy_edges[1:]
        table["e_ref"] = np.sqrt(table["e_min"] * table["e_max"])

        fgl_cols = ["Flux_Band", "Unc_Flux_Band", "Sqrt_TS_Band", "nuFnu_Band"]
        flux, flux_err, sig, nuFnu = [fp_data[col] for col in fgl_cols]

        table["flux"] = flux
        table["flux_errn"] = np.abs(flux_err[:, 0])
        table["flux_errp"] = flux_err[:, 1]

        table["e2dnde"] = nuFnu
        table["e2dnde_errn"] = np.abs(nuFnu * flux_err[:, 0] / flux)
        table["e2dnde_errp"] = nuFnu * flux_err[:, 1] / flux

        is_ul = np.isnan(flux_err[:, 0]) | (sig < 2)
        table["is_ul"] = is_ul

        table["flux_ul"] = np.nan * flux_err.unit
        flux_ul = compute_flux_points_ul(table["flux"], table["flux_errp"])
        table["flux_ul"][is_ul] = flux_ul[is_ul]

        table["e2dnde_ul"] = np.nan * table["e2dnde"].unit
        e2dnde_ul = compute_flux_points_ul(table["e2dnde"], table["e2dnde_errp"])
        table["e2dnde_ul"][is_ul] = e2dnde_ul[is_ul]

        table["sqrt_ts"] = fp_data["Sqrt_TS_Band"]

        return table


class SourceCatalog3FGL(SourceCatalog):
    """Fermi-LAT 3FGL source catalog.

    - https://ui.adsabs.harvard.edu/abs/2015ApJS..218...23A
    - https://fermi.gsfc.nasa.gov/ssc/data/access/lat/4yr_catalog/

    One source is represented by `~gammapy.catalog.SourceCatalogObject3FGL`.
    """

    tag = "3fgl"
    description = "LAT 4-year point source catalog"
    source_object_class = SourceCatalogObject3FGL

    def __init__(self, filename="$GAMMAPY_DATA/catalogs/fermi/gll_psc_v16.fit.gz"):
        filename = make_path(filename)

        with warnings.catch_warnings():  # ignore FITS units warnings
            warnings.simplefilter("ignore", u.UnitsWarning)
            table = Table.read(filename, hdu="LAT_Point_Source_Catalog")

        table_standardise_units_inplace(table)

        source_name_key = "Source_Name"
        source_name_alias = (
            "Extended_Source_Name",
            "0FGL_Name",
            "1FGL_Name",
            "2FGL_Name",
            "1FHL_Name",
            "ASSOC_TEV",
            "ASSOC1",
            "ASSOC2",
        )
        super().__init__(
            table=table,
            source_name_key=source_name_key,
            source_name_alias=source_name_alias,
        )

        self.extended_sources_table = Table.read(filename, hdu="ExtendedSources")
        self.hist_table = Table.read(filename, hdu="Hist_Start")


class SourceCatalog4FGL(SourceCatalog):
    """Fermi-LAT 4FGL source catalog.

    - https://arxiv.org/abs/1902.10045 (DR1)
    - https://arxiv.org/abs/2005.11208 (DR2)
    - https://arxiv.org/abs/2201.11184 (DR3)
    - https://arxiv.org/abs/2307.12546 (DR4)

    By default we use the file of the DR4 initial release
    from https://fermi.gsfc.nasa.gov/ssc/data/access/lat/14yr_catalog/

    One source is represented by `~gammapy.catalog.SourceCatalogObject4FGL`.
    """

    tag = "4fgl"
    description = "LAT 14-year point source catalog"
    source_object_class = SourceCatalogObject4FGL

    def __init__(self, filename="$GAMMAPY_DATA/catalogs/fermi/gll_psc_v32.fit.gz"):
        filename = make_path(filename)
        table = Table.read(filename, hdu="LAT_Point_Source_Catalog")
        table_standardise_units_inplace(table)

        source_name_key = "Source_Name"
        source_name_alias = (
            "Extended_Source_Name",
            "ASSOC_FGL",
            "ASSOC_FHL",
            "ASSOC_GAM1",
            "ASSOC_GAM2",
            "ASSOC_GAM3",
            "ASSOC_TEV",
            "ASSOC1",
            "ASSOC2",
        )
        super().__init__(
            table=table,
            source_name_key=source_name_key,
            source_name_alias=source_name_alias,
        )

        self.extended_sources_table = Table.read(filename, hdu="ExtendedSources")
        self.extended_sources_table["version"] = int(
            "".join(filter(str.isdigit, table.meta["VERSION"]))
        )
        try:
            self.hist_table = Table.read(filename, hdu="Hist_Start")
            if "MJDREFI" not in self.hist_table.meta:
                self.hist_table.meta = Table.read(filename, hdu="GTI").meta
        except KeyError:
            pass
        try:
            self.hist2_table = Table.read(filename, hdu="Hist2_Start")
            if "MJDREFI" not in self.hist_table.meta:
                self.hist2_table.meta = Table.read(filename, hdu="GTI").meta
        except KeyError:
            pass

        table = Table.read(filename, hdu="EnergyBounds")
        self.flux_points_energy_edges = np.unique(
            np.c_[table["LowerEnergy"].quantity, table["UpperEnergy"].quantity]
        )


class SourceCatalog2FHL(SourceCatalog):
    """Fermi-LAT 2FHL source catalog.

    - https://ui.adsabs.harvard.edu/abs/2016ApJS..222....5A
    - https://fermi.gsfc.nasa.gov/ssc/data/access/lat/2FHL/

    One source is represented by `~gammapy.catalog.SourceCatalogObject2FHL`.
    """

    tag = "2fhl"
    description = "LAT second high-energy source catalog"
    source_object_class = SourceCatalogObject2FHL

    def __init__(self, filename="$GAMMAPY_DATA/catalogs/fermi/gll_psch_v09.fit.gz"):
        filename = make_path(filename)

        with warnings.catch_warnings():  # ignore FITS units warnings
            warnings.simplefilter("ignore", u.UnitsWarning)
            table = Table.read(filename, hdu="2FHL Source Catalog")

        table_standardise_units_inplace(table)

        source_name_key = "Source_Name"
        source_name_alias = ("ASSOC", "3FGL_Name", "1FHL_Name", "TeVCat_Name")
        super().__init__(
            table=table,
            source_name_key=source_name_key,
            source_name_alias=source_name_alias,
        )

        self.extended_sources_table = Table.read(filename, hdu="Extended Sources")
        self.rois = Table.read(filename, hdu="ROIs")


class SourceCatalog3FHL(SourceCatalog):
    """Fermi-LAT 3FHL source catalog.

    - https://ui.adsabs.harvard.edu/abs/2017ApJS..232...18A
    - https://fermi.gsfc.nasa.gov/ssc/data/access/lat/3FHL/

    One source is represented by `~gammapy.catalog.SourceCatalogObject3FHL`.
    """

    tag = "3fhl"
    description = "LAT third high-energy source catalog"
    source_object_class = SourceCatalogObject3FHL

    def __init__(self, filename="$GAMMAPY_DATA/catalogs/fermi/gll_psch_v13.fit.gz"):
        filename = make_path(filename)

        with warnings.catch_warnings():  # ignore FITS units warnings
            warnings.simplefilter("ignore", u.UnitsWarning)
            table = Table.read(filename, hdu="LAT_Point_Source_Catalog")

        table_standardise_units_inplace(table)

        source_name_key = "Source_Name"
        source_name_alias = ("ASSOC1", "ASSOC2", "ASSOC_TEV", "ASSOC_GAM")
        super().__init__(
            table=table,
            source_name_key=source_name_key,
            source_name_alias=source_name_alias,
        )

        self.extended_sources_table = Table.read(filename, hdu="ExtendedSources")
        self.rois = Table.read(filename, hdu="ROIs")
        self.energy_bounds_table = Table.read(filename, hdu="EnergyBounds")


class SourceCatalog2PC(SourceCatalog):
    """Fermi-LAT 2nd pulsar catalog.

    - https://ui.adsabs.harvard.edu/abs/2013ApJS..208...17A
    - https://fermi.gsfc.nasa.gov/ssc/data/access/lat/2nd_PSR_catalog/

    One source is represented by `~gammapy.catalog.SourceCatalogObject2PC`.
    """

    tag = "2PC"
    description = "LAT 2nd pulsar catalog"
    source_object_class = SourceCatalogObject2PC

    def __init__(self, filename="$GAMMAPY_DATA/catalogs/fermi/2PC_catalog_v04.fits.gz"):
        filename = make_path(filename)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", u.UnitsWarning)
            table_psr = Table.read(filename, hdu="PULSAR_CATALOG")
            table_spectral = Table.read(filename, hdu="SPECTRAL")
            table_off_peak = Table.read(filename, hdu="OFF_PEAK")

        table_standardise_units_inplace(table_psr)
        table_standardise_units_inplace(table_spectral)
        table_standardise_units_inplace(table_off_peak)

        source_name_key = "PSR_Name"

        super().__init__(table=table_psr, source_name_key=source_name_key)

        self.source_object_class._source_name_key = source_name_key

        self.off_peak_table = table_off_peak
        self.spectral_table = table_spectral

    def to_models(self, **kwargs):
        models = Models()
        for m in self:
            sky_model = m.sky_model()
            if sky_model is not None:
                models.append(sky_model)
        return models

    def _get_name_spectral(self, data):
        return f"{data[self._source_name_key].strip()}"


class SourceCatalog3PC(SourceCatalog):
    """Fermi-LAT 3rd pulsar catalog.

    - https://arxiv.org/abs/2307.11132
    - https://fermi.gsfc.nasa.gov/ssc/data/access/lat/3rd_PSR_catalog/

    One source is represented by `~gammapy.catalog.SourceCatalogObject3PC`.
    """

    tag = "3PC"
    description = "LAT 3rd pulsar catalog"
    source_object_class = SourceCatalogObject3PC

    def __init__(
        self, filename="$GAMMAPY_DATA/catalogs/fermi/3PC_Catalog+SEDs_20230803.fits.gz"
    ):
        filename = make_path(filename)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", u.UnitsWarning)
            table_psr = Table.read(filename, hdu="PULSARS_BIGFILE")
            table_spectral = Table.read(filename, hdu="LAT_Point_Source_Catalog")
            table_bigfile_config = Table.read(filename, hdu="BIGFILE_CONFIG")

        table_standardise_units_inplace(table_psr)
        table_standardise_units_inplace(table_spectral)
        table_standardise_units_inplace(table_bigfile_config)

        source_name_key = "PSRJ"
        super().__init__(table=table_psr, source_name_key=source_name_key)

        self.source_object_class._source_name_key = source_name_key

        self.spectral_table = table_spectral
        self.off_bigfile_config = table_bigfile_config

    def to_models(self, **kwargs):
        models = Models()
        for m in self:
            sky_model = m.sky_model()
            if sky_model is not None:
                models.append(sky_model)
        return models

    @property
    def _get_source_name_key(self):
        return "NickName"

    def _get_name_spectral(self, data):
        return f"PSR{data[self._source_name_key].strip()}"
