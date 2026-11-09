"""Render the shipped rooms template instead of returning a canned HA response."""
import ast
import json
import sys
from pathlib import Path

from jinja2.sandbox import ImmutableSandboxedEnvironment


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/bridge.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name)
                              and target.id == "_SCHEDULE_AREAS_TEMPLATE"
                              for target in node.targets))
    template = ImmutableSandboxedEnvironment().from_string(ast.literal_eval(assignment.value))
    fixtures = [[], [{"id": "living", "name": "客厅", "entities": ["light.main"]}],
                [{"id": "living", "name": "客厅", "entities": ["light.main", "switch.tv"]},
                 {"id": "bedroom", "name": "卧室", "entities": []}]]
    for rows in fixtures:
        by_id = {row["id"]: row for row in rows}
        rendered = template.render(areas=lambda: list(by_id),
                                   area_name=lambda aid: by_id[aid]["name"],
                                   area_entities=lambda aid: by_id[aid]["entities"])
        assert json.loads(rendered) == rows, (rows, rendered)
    print("schedule template tests passed (empty, one room, multiple rooms)")


if __name__ == "__main__":
    main()
