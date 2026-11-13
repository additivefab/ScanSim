"""Mean and sampled-variance comparisons never assert spectra or synchronized captures."""


def write_radiometry_residuals(result, path):
    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise ValueError("radiometry residual plots require zlscan[ident]") from exc
    with rc_context({"svg.hashsalt": "zlscan-radiometry-identification", "svg.fonttype": "none"}):
        figure = Figure(figsize=(10, 7), layout="constrained")
        FigureCanvasAgg(figure)
        axes = figure.subplots(2, 2)
        for column, partition in enumerate(("fit", "validation")):
            rows = [r for r in result["observations"] if r["partition"] == partition]
            mean = [
                r["prediction"]
                for r in rows
                if r.get("prediction", {}).get("mean_residual_si") is not None
                and r["prediction"]["invalid_reason"] is None
            ]
            noise = [
                r["prediction"]
                for r in rows
                if r.get("prediction", {}).get("variance_ratio") is not None
                and r["prediction"]["invalid_reason"] is None
            ]
            if mean:
                axes[0, column].scatter(
                    [r["mean_predicted_si"] for r in mean],
                    [r["mean_residual_si"] for r in mean],
                    s=16,
                )
            else:
                axes[0, column].text(
                    0.5,
                    0.5,
                    "No eligible mean inference",
                    ha="center",
                    transform=axes[0, column].transAxes,
                )
            if noise:
                axes[1, column].scatter(
                    [r["mean_predicted_si"] for r in noise],
                    [r["variance_ratio"] for r in noise],
                    s=16,
                )
            else:
                axes[1, column].text(
                    0.5,
                    0.5,
                    "No eligible variance inference",
                    ha="center",
                    transform=axes[1, column].transAxes,
                )
            axes[0, column].axhline(0, color="#777", linewidth=0.8)
            axes[1, column].axhline(1, color="#777", linewidth=0.8)
            axes[0, column].set_title(
                f"{partition}: mean residuals ({len(rows) - len(mean)} excluded)"
            )
            axes[1, column].set_title(
                f"{partition}: variance ratio ({len(rows) - len(noise)} excluded)"
            )
            axes[0, column].set_ylabel(f"Mean residual ({result['response_unit']})")
            axes[1, column].set_ylabel("Variance ratio (1); not spectral density")
            for axis in axes[:, column]:
                axis.set_xlabel(f"Predicted mean ({result['response_unit']})")
        kinds = ",".join(sorted({r["source_kind"] for r in result["observations"]}))
        figure.suptitle(
            f"{kinds}: conditional radiometry; fit={result['status']}\n"
            f"validation={result['validation']['status']}; no hardware acceptance",
            fontsize=10,
        )
        figure.savefig(path, metadata={"Date": None})
        figure.savefig(path.with_suffix(".png"), dpi=130, metadata={"Software": "zlscan"})
