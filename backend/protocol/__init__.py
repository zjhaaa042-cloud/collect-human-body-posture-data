"""RealAnthro-RGBD-v1 采集协议公共 API。"""

from .conditions import (
    full31_no_lux,
    full36,
    gemini27,
    generate_full31_no_lux,
    generate_full36,
    generate_gemini27,
    generate_primary3,
    primary3,
    validate_conditions,
)
from .measurements import (
    MEASUREMENTS_BY_ID,
    measurement_definitions,
    optional_measurements,
    required_measurements,
)
from .models import Condition, MeasurementDefinition
from .naming import (
    format_capture_stem,
    format_condition_id,
    format_modality_filename,
    parse_capture_stem,
    validate_subject_id,
)

__all__ = [
    "Condition",
    "MeasurementDefinition",
    "MEASUREMENTS_BY_ID",
    "primary3",
    "gemini27",
    "full31_no_lux",
    "full36",
    "generate_primary3",
    "generate_gemini27",
    "generate_full31_no_lux",
    "generate_full36",
    "validate_conditions",
    "measurement_definitions",
    "required_measurements",
    "optional_measurements",
    "validate_subject_id",
    "format_condition_id",
    "format_capture_stem",
    "format_modality_filename",
    "parse_capture_stem",
]
