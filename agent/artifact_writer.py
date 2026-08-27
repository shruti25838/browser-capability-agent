"""ArtifactWriter: serializes a discovery run into the typed Artifact schema.

Generalization from concrete literals to named parameters is driven by what
the LLM declared at `finish` time (DiscoveryRun.parameter_candidates) --
ArtifactWriter's job is mechanical substitution and shaping into pydantic
models, not inference.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from .artifact import (
    Artifact,
    Condition,
    ConditionKind,
    Locator,
    LocatorKind,
    Metadata,
    OutcomeRule,
    OutputField,
    Parameter,
    Step,
    StepType,
)
from .discovery_agent import DiscoveryRun


class ArtifactWriter:
    def build(self, run: DiscoveryRun, scope_selector: Optional[str] = None) -> Artifact:
        if not run.success:
            raise ValueError(f"Refusing to build an artifact from a failed discovery run: {run.reason}")

        value_to_param = {p.value: p.name for p in run.parameter_candidates}

        # Reverse map of concrete extracted value -> output field name, so we can tell a
        # condition asserting "text equals <this run's extracted value>" apart from a
        # condition asserting a genuinely fixed constant. The former is discovery-run-specific
        # (it will differ for any other input) and must be generalized; the latter is stable
        # across replays and should stay a literal.
        #
        # Two sources feed this map: `run.outputs` is the model's own self-report at finish
        # time; each extract step's `output_value` is ground truth captured directly from the
        # Playwright call during execution, independent of whether the model echoes it back
        # correctly (or at all). The latter is a backstop -- self-report can drift or be
        # incomplete, but what was actually read off the page during this run is always
        # exactly what must not be hardcoded.
        output_value_to_field = {value: field for field, value in run.outputs.items()}
        for es in run.steps:
            if es.type == "extract" and es.output_field and es.output_value:
                output_value_to_field.setdefault(es.output_value, es.output_field)
        output_field_to_type = {
            es.output_field: es.output_type for es in run.steps if es.type == "extract" and es.output_field
        }

        def sub(value):
            if value is None:
                return None
            return f"{{{{{value_to_param[value]}}}}}" if value in value_to_param else value

        def condition(d: dict) -> Condition:
            return self._condition_from_dict(d, sub, output_value_to_field, output_field_to_type)

        steps_out: list[Step] = []
        outputs_out: list[OutputField] = []
        seen_outputs: set[str] = set()

        for es in run.steps:
            locator_obj = None
            if es.locator:
                locator_dict = dict(es.locator)
                if locator_dict.get("name"):
                    locator_dict["name"] = sub(locator_dict["name"])
                allowed = set(Locator.model_fields)
                locator_obj = Locator(**{k: v for k, v in locator_dict.items() if k in allowed})

            outcome_mapping_out = [
                OutcomeRule(outcome_name=rule["outcome_name"], condition=condition(rule["condition"]))
                for rule in (es.outcome_mapping or [])
            ]

            steps_out.append(
                Step(
                    order=es.order,
                    type=StepType(es.type),
                    description=es.description,
                    precondition=condition(es.precondition),
                    postcondition=condition(es.postcondition),
                    locator=locator_obj,
                    contains_text=sub(es.contains_text),
                    input_text=sub(es.input_text),
                    output_field=es.output_field,
                    outcome_mapping=outcome_mapping_out,
                )
            )

            if es.type == "extract" and es.output_field and es.output_field not in seen_outputs:
                outputs_out.append(
                    OutputField(name=es.output_field, type=es.output_type, description=es.description)
                )
                seen_outputs.add(es.output_field)

        parameters_out = [
            Parameter(name=p.name, type=p.type, description=p.description, example_value=p.value)
            for p in run.parameter_candidates
        ]

        return Artifact(
            metadata=Metadata(goal=run.goal, target_url=run.target_url, created_at=datetime.now(timezone.utc)),
            scope_selector=scope_selector,
            parameters=parameters_out,
            outputs=outputs_out,
            steps=steps_out,
            success_condition=condition(run.success_condition or {}),
        )

    def write(self, run: DiscoveryRun, output_dir: str, scope_selector: Optional[str] = None) -> str:
        artifact = self.build(run, scope_selector=scope_selector)
        os.makedirs(output_dir, exist_ok=True)
        filename = f"{self._slugify(run.goal)}_{int(time.time())}.json"
        path = os.path.join(output_dir, filename)
        artifact.to_json_file(path)
        return path

    @staticmethod
    def _condition_from_dict(
        d: dict,
        sub,
        output_value_to_field: Optional[dict[str, str]] = None,
        output_field_to_type: Optional[dict[str, str]] = None,
    ) -> Condition:
        locator = None
        if d.get("column_index") is not None:
            locator = Locator(kind=LocatorKind.TABLE_CELL_BY_COLUMN, column_index=d["column_index"], scope="row")
        elif d.get("column_header"):
            locator = Locator(kind=LocatorKind.TABLE_CELL_BY_COLUMN, column_header=d["column_header"], scope="row")
        elif d.get("role"):
            locator = Locator(kind=LocatorKind.ROLE, role=d.get("role"), name=sub(d.get("name")))

        kind = d["kind"]
        expected_value = d.get("expected_value")
        contains_text = d.get("contains_text")

        # The discovery-time LLM has no way to know whether the literal it asserted is a
        # fixed constant (stable across replays) or the very value an extract step just
        # read (which varies with the input and must never be baked in). Detect the latter
        # mechanically: if the asserted literal (in either expected_value or contains_text --
        # models are inconsistent about which field they put a text_contains literal in) is
        # exactly one of this run's extracted outputs, it can only be describing that
        # extraction, so replace the equality/substring check with "the output field holds a
        # non-empty value of the expected type" instead of hardcoding this run's incidental
        # result.
        output_field = None
        if kind in ("text_equals", "text_contains"):
            value_map = output_value_to_field or {}
            output_field = value_map.get(expected_value)
            if output_field is None:
                output_field = value_map.get(contains_text)
        if output_field is not None:
            output_type = (output_field_to_type or {}).get(output_field, "string")
            return Condition(
                kind=ConditionKind.VALUE_EXTRACTED,
                description=f"{output_field} was extracted as a non-empty {output_type} value",
                locator=locator,
                output_field=output_field,
                expected_type=output_type,
            )

        return Condition(
            kind=ConditionKind(kind),
            description=d["description"],
            locator=locator,
            contains_text=sub(d.get("contains_text")),
            expected_value=sub(expected_value),
            url_pattern=d.get("url_pattern"),
        )

    @staticmethod
    def _slugify(text: str, max_len: int = 40) -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
        return slug[:max_len] or "artifact"
