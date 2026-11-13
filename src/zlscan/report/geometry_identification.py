"""Residuals preserve fit/holdout partitions and unresolved landmarks."""


def write_geometry_residuals(result, path):
    try:
        from matplotlib import rc_context
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure
    except ImportError as exc:
        raise ValueError("geometry residual plots require zlscan[ident]") from exc
    with rc_context({"svg.hashsalt": "zlscan-geometry-identification", "svg.fonttype": "none"}):
        figure = Figure(figsize=(9, 6), layout="constrained")
        FigureCanvasAgg(figure)
        axes = figure.subplots(2, 1)
        for axis, partition in zip(axes, ("fit", "validation"), strict=True):
            omitted = 0
            for phase, fit in result["facets"].items():
                rows = [r for r in fit["rows"] if r["partition"] == partition]
                valid = [r for r in rows if r["residual_m"] is not None]
                omitted += len(rows) - len(valid)
                if valid:
                    axis.scatter(
                        [r["u"] for r in valid],
                        [r["residual_m"] for r in valid],
                        s=12,
                        label=f"phase {phase}",
                    )
            omitted += sum(
                p["partition"] == partition for p in result["excluded_unresolved_landmarks"]
            )
            axis.axhline(0, color="#777", linewidth=0.8)
            axis.set_title(f"{partition}: excluded/unidentifiable landmarks = {omitted}")
            axis.set_xlabel("Declared normalized sample coordinate u")
            axis.set_ylabel("Target-x residual (m)")
            if axis.collections:
                axis.legend()
            else:
                axis.text(
                    0.5, 0.5, "No identifiable residuals", transform=axis.transAxes, ha="center"
                )
        kinds = ",".join(sorted({r["source_kind"] for r in result["sources"]}))
        figure.suptitle(
            f"{kinds}: conditional geometry estimate; no hardware acceptance", fontsize=10
        )
        figure.savefig(path, metadata={"Date": None})
        figure.savefig(path.with_suffix(".png"), dpi=130, metadata={"Software": "zlscan"})
