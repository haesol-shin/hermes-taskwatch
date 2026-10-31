"""A spec that satisfies the spec template check, for jobs of review `code` or `design`."""

from taskwatch.jobs import SPEC_HEADINGS

VALID_SPEC = "# Spec\n\n" + "".join(
    f"## {heading}\n\nn/a: synthetic fixture\n\n" for heading in SPEC_HEADINGS
)
