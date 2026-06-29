"""
GhostNodes Developer SDK — scaffold, test, validate, and publish AI nodes.

Provides tools for:
  - Creating new node projects from templates
  - Validating NODE.yaml manifests
  - Running node test suites
  - Packaging nodes for distribution
"""

import json
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path

log = logging.getLogger("quinely.node_sdk")

GHOST_HOME = Path.home() / ".ghost"
NODES_DIR = GHOST_HOME / "nodes"


NODE_YAML_TEMPLATE = textwrap.dedent("""\
    name: {name}
    version: 0.1.0
    description: "{description}"
    author: {author}
    category: {category}
    license: MIT

    requires:
      python: ">=3.10"
      gpu: {gpu}
      vram_gb: {vram_gb}
      disk_gb: 0.5
      deps: {deps}

    models: []

    tools:
      - {tool_name}

    inputs: [{input_type}]
    outputs: [{output_type}]

    tags: [{tags}]
""")

NODE_PY_TEMPLATE = textwrap.dedent('''\
    """
    {name} Node — {description}
    """

    import json
    import logging
    import time

    log = logging.getLogger("quinely.node.{name_safe}")


    def register(api):
        """Register tools with Ghost."""

        def execute_{tool_name}(**kwargs):
            """Main tool implementation."""
            # TODO: Implement your tool logic here
            api.log("Running {tool_name}...")
            t0 = time.time()

            try:
                # Your implementation goes here
                result = {{"message": "Hello from {name}!"}}
                elapsed = time.time() - t0

                return json.dumps({{
                    "status": "ok",
                    "result": result,
                    "elapsed_secs": round(elapsed, 2),
                }})

            except Exception as e:
                return json.dumps({{"status": "error", "error": str(e)[:500]}})

        api.register_tool({{
            "name": "{tool_name}",
            "description": "{description}",
            "parameters": {{
                "type": "object",
                "properties": {{
                    "input": {{
                        "type": "string",
                        "description": "Input for the tool",
                    }},
                }},
                "required": ["input"],
            }},
            "execute": execute_{tool_name},
        }})
''')

TEST_PY_TEMPLATE = textwrap.dedent('''\
    """Tests for {name} node."""

    import json
    import os
    import sys
    from pathlib import Path
    from unittest.mock import MagicMock

    # Find Ghost project root: walk up until we find ghost_node_manager.py
    _here = Path(__file__).resolve().parent
    for _ancestor in [_here.parent.parent, _here.parent.parent.parent, _here.parent]:
        if (_ancestor / "ghost_node_manager.py").exists():
            sys.path.insert(0, str(_ancestor))
            break
    else:
        # Fallback: check GHOST_ROOT env var
        _root = os.environ.get("GHOST_ROOT", "")
        if _root:
            sys.path.insert(0, _root)

    try:
        from ghost_node_manager import NodeManifest, NodeAPI
        from ghost_resource_manager import ResourceManager
        _HAS_GHOST = True
    except ImportError:
        _HAS_GHOST = False


    def make_test_api(node_dir):
        """Create a mock NodeAPI for testing."""
        if not _HAS_GHOST:
            raise RuntimeError(
                "Ghost core not found. Set GHOST_ROOT env var to your Ghost install dir, "
                "or run from within the Ghost project."
            )
        manifest = NodeManifest.from_yaml(node_dir / "NODE.yaml")
        resource_manager = ResourceManager()
        return NodeAPI(
            node_id=manifest.name,
            manifest=manifest,
            tool_registry=MagicMock(),
            resource_manager=resource_manager,
            media_store=MagicMock(),
            config={{}},
        )


    def test_register():
        """Test that the node registers its tools."""
        node_dir = Path(__file__).parent
        api = make_test_api(node_dir)

        from node import register
        register(api)

        assert len(api._registered_tools) > 0, "Node must register at least one tool"
        print(f"  Registered tools: {{api._registered_tools}}")


    def test_manifest():
        """Test that NODE.yaml is valid."""
        if not _HAS_GHOST:
            print("  Skipped (Ghost core not found)")
            return
        node_dir = Path(__file__).parent
        manifest = NodeManifest.from_yaml(node_dir / "NODE.yaml")

        assert manifest.name, "name is required"
        assert manifest.version, "version is required"
        assert manifest.category, "category is required"
        assert manifest.tools, "tools list is required"
        print(f"  Manifest valid: {{manifest.name}} v{{manifest.version}}")


    if __name__ == "__main__":
        print(f"Testing {name} node...")
        test_manifest()
        test_register()
        print("All tests passed!")
''')

README_TEMPLATE = textwrap.dedent("""\
    # {name}

    {description}

    ## Installation

    {install_text}

    ```
    # Via Ghost chat:
    > Install the {name} node

    # Or via dashboard:
    # Go to AI Nodes > Install > enter the source
    ```

    ## Tools

    | Tool | Description |
    |------|-------------|
    | `{tool_name}` | {description} |

    ## Requirements

    - Python {python_req}
    - {gpu_text}
    {deps_section}

    ## Development

    ```bash
    # Run tests
    python test_node.py
    ```

    ## License

    {license}
""")


def scaffold_node(name: str, description: str = "", category: str = "utility",
                   author: str = "", gpu: bool = False, output_dir: str = "") -> dict:
    """Create a new node project from template."""
    name_safe = name.replace("-", "_").replace(" ", "_").lower()
    tool_name = name_safe

    target = Path(output_dir) if output_dir else NODES_DIR / name
    if target.exists():
        return {"status": "error", "error": f"Directory already exists: {target}"}

    target.mkdir(parents=True, exist_ok=True)

    io_map = {
        "image_generation": ("text", "image"),
        "video": ("text", "video"),
        "audio": ("text", "audio"),
        "vision": ("image", "text"),
        "llm": ("text", "text"),
        "3d": ("image", "mesh_3d"),
        "data": ("text", "json"),
        "utility": ("text", "text"),
    }
    input_type, output_type = io_map.get(category, ("text", "text"))

    (target / "NODE.yaml").write_text(NODE_YAML_TEMPLATE.format(
        name=name, description=description or f"A {category} node",
        author=author or "community", category=category,
        gpu="true" if gpu else "false",
        vram_gb=4 if gpu else 0,
        deps='["torch"]' if gpu else '[]',
        tool_name=tool_name,
        input_type=f'"{input_type}"', output_type=f'"{output_type}"',
        tags=f'"{name}", "{category}"',
    ), encoding="utf-8")

    (target / "node.py").write_text(NODE_PY_TEMPLATE.format(
        name=name, name_safe=name_safe, description=description or f"A {category} node",
        tool_name=tool_name,
    ), encoding="utf-8")

    (target / "test_node.py").write_text(TEST_PY_TEMPLATE.format(name=name), encoding="utf-8")

    (target / "README.md").write_text(README_TEMPLATE.format(
        name=name, description=description or f"A {category} node",
        install_text="Installed via the GhostNodes registry",
        tool_name=tool_name,
        python_req=">=3.10",
        gpu_text="GPU recommended" if gpu else "CPU only",
        deps_section="- torch (for GPU)" if gpu else "",
        license="MIT",
    ), encoding="utf-8")

    (target / "requirements.txt").write_text(
        "torch\n" if gpu else "# Add dependencies here\n",
        encoding="utf-8",
    )

    return {
        "status": "ok",
        "path": str(target),
        "files": ["NODE.yaml", "node.py", "test_node.py", "README.md", "requirements.txt"],
    }


def validate_node(node_dir: str) -> dict:
    """Validate a node directory has correct structure and manifest."""
    path = Path(node_dir)
    errors = []
    warnings = []

    if not path.is_dir():
        return {"valid": False, "errors": [f"Not a directory: {node_dir}"]}

    manifest_path = path / "NODE.yaml"
    if not manifest_path.exists():
        manifest_path = path / "NODE.yml"
    if not manifest_path.exists():
        errors.append("Missing NODE.yaml manifest")
    else:
        try:
            from ghost_node_manager import NodeManifest
            manifest = NodeManifest.from_yaml(manifest_path)
            if not manifest.name:
                errors.append("Manifest missing 'name'")
            if not manifest.tools:
                warnings.append("Manifest has no tools listed")
            if not manifest.description:
                warnings.append("Manifest has no description")
        except Exception as e:
            errors.append(f"Invalid manifest: {e}")

    if not (path / "node.py").exists():
        errors.append("Missing node.py (entry point)")

    node_py = path / "node.py"
    if node_py.exists():
        content = node_py.read_text(encoding="utf-8")
        if "def register(" not in content:
            errors.append("node.py must export a register(api) function")

    return {
        "valid": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "path": str(path),
    }


def test_node(node_dir: str) -> dict:
    """Run a node's test suite."""
    path = Path(node_dir)
    test_file = path / "test_node.py"

    if not test_file.exists():
        return {"status": "skipped", "message": "No test_node.py found"}

    try:
        result = subprocess.run(
            [sys.executable, str(test_file)],
            capture_output=True, text=True, timeout=120,
            cwd=str(path),
        )
        return {
            "status": "ok" if result.returncode == 0 else "failed",
            "returncode": result.returncode,
            "stdout": result.stdout[-1000:],
            "stderr": result.stderr[-500:],
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "message": "Tests timed out after 120s"}
    except Exception as e:
        return {"status": "error", "error": str(e)[:300]}


# ═════════════════════════════════════════════════════════════════════
#  TOOL BUILDER
# ═════════════════════════════════════════════════════════════════════

def build_node_sdk_tools():
    """Build tools for node development."""

    def execute_create(name="", description="", category="utility",
                       author="", gpu=False, **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name is required"})
        result = scaffold_node(name, description, category, author, gpu)
        return json.dumps(result, default=str)

    def execute_validate(path="", **_kw):
        if not path:
            return json.dumps({"status": "error", "error": "path is required"})
        result = validate_node(path)
        return json.dumps(result, default=str)

    def execute_test(path="", **_kw):
        if not path:
            return json.dumps({"status": "error", "error": "path is required"})
        result = test_node(path)
        return json.dumps(result, default=str)

    return [
        {
            "name": "node_create",
            "description": (
                "Create a new GhostNode project from template. "
                "Scaffolds NODE.yaml, node.py, test_node.py, README.md, and requirements.txt."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Node name (e.g. 'my-image-filter')"},
                    "description": {"type": "string", "description": "Short description of what the node does"},
                    "category": {
                        "type": "string",
                        "enum": ["image_generation", "video", "audio", "vision", "llm", "3d", "data", "utility"],
                        "description": "Node category",
                    },
                    "author": {"type": "string", "description": "Author name"},
                    "gpu": {"type": "boolean", "description": "Whether this node needs GPU (default false)"},
                },
                "required": ["name"],
            },
            "execute": execute_create,
        },
        {
            "name": "node_validate",
            "description": "Validate a GhostNode directory (checks manifest, entry point, structure).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the node directory"},
                },
                "required": ["path"],
            },
            "execute": execute_validate,
        },
        {
            "name": "node_test",
            "description": "Run a GhostNode's test suite (test_node.py).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the node directory"},
                },
                "required": ["path"],
            },
            "execute": execute_test,
        },
    ]
