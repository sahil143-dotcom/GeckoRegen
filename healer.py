"""
GeckoRegen — Healer + Validator.

Contextual prompt generation → BD Self-Healing API → validation gate.
The core differentiator: we don't trust BD blindly — every heal is re-run
and schema-checked before acceptance.

Flow:  generate_heal_prompt  →  trigger_and_poll_heal  →  approve  →  validate_heal
       (max 3 attempts, progressively sharper prompts)
"""
import time
import bd_client

BD_PROMPT_LIMIT = 1000


# ── 1. Prompt generation ────────────────────────────────────────────

def generate_heal_prompt(regulator_name, broken_fields, last_known_good):
    """Build a contextual heal prompt. Stays under BD's 1000-char limit.

    Args:
        regulator_name:  e.g. "FCA"
        broken_fields:   ["title", "publish_date", "summary"]
        last_known_good: {"title": "FCA fines XYZ Ltd", "publish_date": "2024-03-15", ...}
    Returns:
        str — the prompt, truncated to BD_PROMPT_LIMIT.
    """
    lines = [f"Fix scraper for {regulator_name} — page structure changed, fields now return null."]
    for field in broken_fields:
        example = last_known_good.get(field)
        if example is None:
            hint = "no sample available"
        elif isinstance(example, str):
            hint = f"string like '{example[:50]}'"
        elif isinstance(example, bool):
            hint = f"boolean like {example}"
        elif isinstance(example, (int, float)):
            hint = f"numeric like {example}"
        else:
            hint = f"value like {example!r}"
        lines.append(f"Field '{field}' returns null on {regulator_name}. Expected: {hint}. Last-known-good: {example!r}. Fix to extract from the new page structure.")
    prompt = " ".join(lines)
    return prompt[:BD_PROMPT_LIMIT]


# ── 2. Heal trigger + poll ───────────────────────────────────────────

def trigger_and_poll_heal(collector_id, prompt, url):
    """Submit heal prompt to BD, poll until pending_answer / done / failed.

    Returns BD's status JSON dict.
    """
    bd_client.trigger_heal(collector_id, prompt, url)
    return bd_client.poll_heal(collector_id)


# ── 3. Validation gate ───────────────────────────────────────────────

def _validate_records(records, expected_schema, min_population_rate=0.9):
    """Pure validation logic — no network.  Returns ValidationResult dict.

    expected_schema: {field_name: "str"|"int"|"float"|"bool"|None}
                     None = don't check type, just population.
    """
    record_count = len(records)

    if record_count == 0:
        return {
            "valid": False,
            "reason": "no records returned",
            "record_count": 0,
            "avg_population_rate": 0.0,
            "format_consistent": False,
            "count_ok": False,
            "field_details": {},
        }

    _type_map = {"str": str, "int": int, "float": (int, float), "bool": bool}

    field_details = {}
    for field, expected_type in expected_schema.items():
        values = [r.get(field) for r in records]
        non_null = [v for v in values if v is not None and v != ""]
        pop_rate = len(non_null) / record_count if record_count else 0.0

        # format consistency: all non-null values share one Python type
        types_seen = {type(v).__name__ for v in non_null}
        format_ok = len(types_seen) <= 1

        # type match against expected schema
        if expected_type and expected_type in _type_map:
            type_ok = all(isinstance(v, _type_map[expected_type]) for v in non_null)
        else:
            type_ok = True  # ponytail: no type constraint = skip check

        field_details[field] = {
            "population_rate": round(pop_rate, 4),
            "format_consistent": format_ok,
            "type_match": type_ok,
            "types_seen": list(types_seen),
        }

    avg_pop = (
        sum(f["population_rate"] for f in field_details.values()) / len(field_details)
        if field_details else 0.0
    )
    all_format_ok = all(f["format_consistent"] for f in field_details.values())
    all_type_ok = all(f["type_match"] for f in field_details.values())

    # ponytail: count_ok = >0 records; add baseline comparison if caller provides one
    count_ok = record_count > 0

    valid = (
        avg_pop >= min_population_rate
        and all_format_ok
        and all_type_ok
        and count_ok
    )

    return {
        "valid": valid,
        "record_count": record_count,
        "avg_population_rate": round(avg_pop, 4),
        "format_consistent": all_format_ok,
        "type_match": all_type_ok,
        "count_ok": count_ok,
        "field_details": field_details,
    }


def validate_heal(collector_id, url, expected_schema, min_population_rate=0.9):
    """Re-run scraper on same URL and validate output against expected schema.

    Returns ValidationResult dict.
    """
    snapshot_id = bd_client.trigger_scraper(collector_id, [url])
    records = bd_client.get_dataset(snapshot_id)
    return _validate_records(records, expected_schema, min_population_rate)


# ── 4. Full pipeline ─────────────────────────────────────────────────

def heal_pipeline(
    collector_id,
    regulator_name,
    broken_fields,
    last_known_good,
    url,
    expected_schema,
    min_population_rate=0.9,
    max_attempts=3,
):
    """Full heal loop: generate prompt → trigger → poll → approve → validate.

    Sharper prompts on each retry.  Returns dict with success/failure + details.
    """
    base_prompt = generate_heal_prompt(regulator_name, broken_fields, last_known_good)
    validation = None

    for attempt in range(1, max_attempts + 1):
        # progressively sharper prompts
        if attempt == 1:
            prompt = base_prompt
        elif attempt == 2:
            prefix = f"PREVIOUS HEAL FAILED VALIDATION (attempt 1). Fields still null — be more specific about the CSS selectors and data attributes. "
            prompt = (prefix + base_prompt)[:BD_PROMPT_LIMIT]
        else:
            prefix = f"CRITICAL: {attempt - 1} attempts failed. Fields are STILL returning null. Carefully inspect the actual HTML DOM structure, identify the new container and class names, and rewrite the selectors from scratch. "
            prompt = (prefix + base_prompt)[:BD_PROMPT_LIMIT]

        # 1. trigger + poll
        heal_result = trigger_and_poll_heal(collector_id, prompt, url)
        status = heal_result.get("status", "")

        if status == "failed":
            continue

        # 2. approve if BD is waiting for human-in-the-loop
        if status == "pending_answer":
            bd_client.approve_heal(collector_id, approve=True)
            heal_result = bd_client.poll_heal(collector_id)
            status = heal_result.get("status", "")

        if status != "done":
            continue

        # 3. validate the healed scraper
        validation = validate_heal(collector_id, url, expected_schema, min_population_rate)

        if validation["valid"]:
            return {
                "success": True,
                "attempts": attempt,
                "heal_result": heal_result,
                "validation": validation,
            }
        # else: retry with sharper prompt

    return {
        "success": False,
        "attempts": max_attempts,
        "reason": "max attempts reached — manual intervention required",
        "last_validation": validation,
    }


# ── Self-check ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== healer.py self-check ===\n")

    # 1. generate_heal_prompt — check structure + char limit
    regulator = "FCA"
    broken = ["title", "publish_date", "summary", "article_url"]
    lkg = {
        "title": "FCA fines XYZ Ltd £2.3M for AML failures",
        "publish_date": "2024-03-15",
        "summary": "The FCA has imposed a financial penalty...",
        "article_url": "https://www.fca.org.uk/news/press-releases/fca-fines-xyz",
    }
    prompt = generate_heal_prompt(regulator, broken, lkg)
    assert len(prompt) <= BD_PROMPT_LIMIT, f"Prompt too long: {len(prompt)}"
    assert "FCA" in prompt, "Regulator name missing"
    assert "title" in prompt, "Broken field missing"
    assert "publish_date" in prompt, "Broken field missing"
    assert "Last-known-good" in prompt, "Last-known-good marker missing"
    print(f"1. generate_heal_prompt: {len(prompt)} chars (limit {BD_PROMPT_LIMIT})")
    print(f"   {prompt[:200]}...\n")

    # 2. _validate_records — happy path (all fields populated, consistent types)
    good_records = [
        {"title": f"News item {i}", "publish_date": f"2024-0{i%9+1}-15", "summary": "text"}
        for i in range(1, 11)
    ]
    schema = {"title": "str", "publish_date": "str", "summary": "str"}
    vr = _validate_records(good_records, schema)
    assert vr["valid"] is True, f"Should be valid: {vr}"
    assert vr["record_count"] == 10
    assert vr["avg_population_rate"] == 1.0
    assert vr["format_consistent"] is True
    print(f"2. validate (good data): valid={vr['valid']} pop={vr['avg_population_rate']} count={vr['record_count']}")

    # 3. _validate_records — broken (some fields null → low population)
    broken_records = [
        {"title": f"News {i}", "publish_date": None, "summary": "text"}
        for i in range(10)
    ]
    vr2 = _validate_records(broken_records, schema, min_population_rate=0.9)
    assert vr2["valid"] is False, f"Should be invalid (low pop): {vr2}"
    assert vr2["field_details"]["publish_date"]["population_rate"] == 0.0
    print(f"3. validate (null field): valid={vr2['valid']} pop={vr2['avg_population_rate']}")

    # 4. _validate_records — format inconsistency (mixed types)
    mixed_records = [
        {"title": "text", "publish_date": "2024-01-01", "summary": "a"},
        {"title": 42, "publish_date": "2024-01-02", "summary": "b"},  # title is int, not str
    ]
    vr3 = _validate_records(mixed_records, schema)
    assert vr3["valid"] is False, f"Should be invalid (type mismatch): {vr3}"
    assert vr3["type_match"] is False
    print(f"4. validate (mixed types): valid={vr3['valid']} type_match={vr3['type_match']}")

    # 5. _validate_records — empty dataset
    vr4 = _validate_records([], schema)
    assert vr4["valid"] is False
    assert vr4["reason"] == "no records returned"
    print(f"5. validate (empty): valid={vr4['valid']} reason={vr4['reason']}")

    # 6. generate_heal_prompt — handles missing last_known_good
    prompt2 = generate_heal_prompt("ESMA", ["field_x"], {})
    assert len(prompt2) <= BD_PROMPT_LIMIT
    assert "ESMA" in prompt2
    assert "no sample available" in prompt2
    print(f"6. generate_heal_prompt (missing LKG): {len(prompt2)} chars")

    # 7. generate_heal_prompt — truncation under extreme input
    many_fields = [f"field_{i}" for i in range(200)]
    many_lkg = {f"field_{i}": f"example_value_{i}" * 20 for i in range(200)}
    prompt3 = generate_heal_prompt("BaFin", many_fields, many_lkg)
    assert len(prompt3) <= BD_PROMPT_LIMIT, f"Should be truncated: {len(prompt3)}"
    print(f"7. generate_heal_prompt (truncated): {len(prompt3)} chars")

    print("\n=== All self-checks passed ===")
