from pathlib import Path

import yaml

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


# A broken header (no closing ---, invalid YAML, or YAML that is not a mapping) is ignored:
# one bad SKILL.md must not stop Excudo from starting (the skill list is read at import).
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    _, frontmatter, body = parts
    try:
        meta = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        meta = None
    return (meta if isinstance(meta, dict) else {}), body.strip()


def discover_skills() -> dict[str, dict]:
    """Scans skills/<name>/SKILL.md for frontmatter only (name + description).

    Deliberately does not read the body here: this keeps the load_skill tool
    schema small (and prefix-cache friendly) even as more skills get added.
    The full body is only read when a skill is actually invoked.
    """
    skills: dict[str, dict] = {}
    if not SKILLS_DIR.exists():
        return skills
    for skill_file in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        try:
            meta, _ = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue  # unreadable: skipped, the other skills still load
        name = meta.get("name") if isinstance(meta.get("name"), str) and meta.get("name") else skill_file.parent.name
        description = meta.get("description")
        skills[name] = {"description": description if isinstance(description, str) else "", "path": skill_file}
    return skills


def load_skill_body(name: str) -> str:
    entry = discover_skills().get(name)
    if not entry:
        return f"Unknown skill: {name}"
    _, body = _parse_frontmatter(entry["path"].read_text(encoding="utf-8"))
    return body
