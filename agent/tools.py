"""Tool schemas offered to the LLM during discovery.

Every action tool requires the model to state a `precondition` and a
`postcondition` as structured Conditions (not free text) -- this is what
lets ArtifactWriter turn a discovery run directly into typed Steps with
almost no inference of its own.
"""

CONDITION_SCHEMA = {
    "type": "object",
    "description": "A single checkable fact about page state.",
    "properties": {
        "kind": {
            "type": "string",
            "enum": [
                "element_visible",
                "element_not_visible",
                "row_visible",
                "row_absent",
                "text_equals",
                "text_contains",
                "url_contains",
            ],
        },
        "description": {"type": "string", "description": "Human-readable statement of what must be true"},
        "role": {"type": "string", "description": "ARIA role of the element this refers to, if applicable"},
        "name": {"type": "string", "description": "Accessible name of the element, if applicable"},
        "column_header": {
            "type": "string",
            "description": "Column header text, if this refers to a table cell located by column",
        },
        "column_index": {
            "type": "integer",
        },
        "contains_text": {
            "type": "string",
            "description": "Text a row must (row_visible) or must no longer (row_absent) contain",
        },
        "expected_value": {"type": "string", "description": "Expected text value for text_equals/text_contains"},
        "url_pattern": {"type": "string", "description": "Substring the URL must contain, for url_contains"},
    },
    "required": ["kind", "description"],
    "additionalProperties": False,
}

OUTCOME_RULE_SCHEMA = {
    "type": "object",
    "description": (
        "A named business outcome for this step: if `condition` is observed to hold when this "
        "step's own precondition/postcondition comes back false, classify the result as that "
        "named business outcome (e.g. 'permission_denied') instead of a generic failure."
    ),
    "properties": {
        "outcome_name": {"type": "string", "description": "snake_case name for this business outcome"},
        "condition": CONDITION_SCHEMA,
    },
    "required": ["outcome_name", "condition"],
    "additionalProperties": False,
}

OUTCOME_MAPPING_SCHEMA = {
    "type": "array",
    "description": (
        "Optional: named business-outcome branches for this step, beyond a generic failure. Only "
        "declare this when the action could plausibly fail for a distinguishable business reason "
        "(e.g. a permission denial, a validation error, an unexpected dialog) rather than a system "
        "break -- most steps don't need this and can omit it."
    ),
    "items": OUTCOME_RULE_SCHEMA,
}

FIND_ROW_TOOL = {
    "name": "find_row",
    "description": (
        "Locate the table row whose content contains the given text, and scope all subsequent "
        "click/type/extract calls to that row until another find_row is called. Never locate a row "
        "by position -- always by content it contains (e.g. a name or email in one of its cells)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "contains_text": {"type": "string", "description": "Text the target row must contain"},
            "precondition": CONDITION_SCHEMA,
            "postcondition": CONDITION_SCHEMA,
            "outcome_mapping": OUTCOME_MAPPING_SCHEMA,
        },
        "required": ["contains_text", "precondition", "postcondition"],
        "additionalProperties": False,
    },
}

CLICK_TOOL = {
    "name": "click",
    "description": (
        "Click an element identified by accessible role + accessible name. If the most recent action "
        "was find_row, this click is scoped to that row (e.g. click the 'delete' link inside the matched "
        "row, not any 'delete' link on the page). Only use css_selector as a fallback when the element has "
        "no usable accessible name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "ARIA role, e.g. 'link', 'button', 'textbox'"},
            "name": {"type": "string", "description": "Accessible name of the element"},
            "name_match": {"type": "string", "enum": ["exact", "contains"], "default": "exact"},
            "css_selector": {"type": "string", "description": "Fallback DOM selector; only if no accessible name exists"},
            "precondition": CONDITION_SCHEMA,
            "postcondition": CONDITION_SCHEMA,
            "outcome_mapping": OUTCOME_MAPPING_SCHEMA,
        },
        "required": ["precondition", "postcondition"],
        "additionalProperties": False,
    },
}

TYPE_TOOL = {
    "name": "type",
    "description": "Type text into an input identified by accessible role + accessible name.",
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "ARIA role, typically 'textbox'"},
            "name": {"type": "string", "description": "Accessible name of the input"},
            "name_match": {"type": "string", "enum": ["exact", "contains"], "default": "exact"},
            "css_selector": {"type": "string", "description": "Fallback DOM selector; only if no accessible name exists"},
            "text": {"type": "string", "description": "Text to type"},
            "precondition": CONDITION_SCHEMA,
            "postcondition": CONDITION_SCHEMA,
            "outcome_mapping": OUTCOME_MAPPING_SCHEMA,
        },
        "required": ["text", "precondition", "postcondition"],
        "additionalProperties": False,
    },
}

EXTRACT_TOOL = {
    "name": "extract",
    "description": (
        "Read text from an element and store it into a named output field. To read a table cell in the "
        "row most recently matched by find_row, use column_header (the cell is found by matching the "
        "column's header text, never by a hardcoded column position). Otherwise identify the element by "
        "accessible role + name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "role": {"type": "string", "description": "ARIA role of the element to read, if not using column_header"},
            "name": {"type": "string", "description": "Accessible name of the element, if not using column_header"},
            "column_header": {
                "type": "string",
                "description": "Header text of the column to read, within the row scoped by the last find_row",
            },
            "column_index": {
                "type": "integer",
            },
            "output_field": {"type": "string", "description": "snake_case name to store the extracted value under"},
            "output_type": {"type": "string", "enum": ["string", "number", "boolean"], "default": "string"},
            "precondition": CONDITION_SCHEMA,
            "postcondition": CONDITION_SCHEMA,
            "outcome_mapping": OUTCOME_MAPPING_SCHEMA,
        },
        "required": ["output_field", "precondition", "postcondition"],
        "additionalProperties": False,
    },
}

FINISH_TOOL = {
    "name": "finish",
    "description": (
        "End the discovery run once the goal has been achieved, or once it is determined to be "
        "impossible. Must declare which concrete literal values used during this run should be "
        "generalized into named input parameters for replay, and the overall success checkpoint for "
        "the whole flow."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "success": {"type": "boolean"},
            "reason": {"type": "string", "description": "Why the run succeeded or failed"},
            "outputs": {
                "type": "object",
                "description": "Map of output_field name -> concrete extracted value observed this run",
                "additionalProperties": {"type": "string"},
            },
            "parameters": {
                "type": "array",
                "description": (
                    "Concrete literal values used during this run that should become named replay "
                    "inputs (e.g. the literal 'Smith' becomes {name: 'last_name', ...})."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "snake_case parameter name"},
                        "value": {
                            "type": "string",
                            "description": "The exact literal value used during this run, verbatim",
                        },
                        "type": {"type": "string", "enum": ["string", "number", "boolean"]},
                        "description": {"type": "string"},
                    },
                    "required": ["name", "value", "type", "description"],
                    "additionalProperties": False,
                },
            },
            "success_condition": CONDITION_SCHEMA,
        },
        "required": ["success", "reason", "outputs", "parameters", "success_condition"],
        "additionalProperties": False,
    },
}

DISCOVERY_TOOLS = [FIND_ROW_TOOL, CLICK_TOOL, TYPE_TOOL, EXTRACT_TOOL, FINISH_TOOL]
