"""clipforge — long video → scored, captioned, motion-graphic short clips."""
from .pipeline import rerender, run_pipeline
from .template import BrandTemplate

__all__ = ["run_pipeline", "rerender", "BrandTemplate"]
__version__ = "0.1.0"
