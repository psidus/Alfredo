# Workflow function blocks, canvas & structured loops

Alfredo workflows use a **function-block schema** stored in `workflows.task_ids_json`.

## Node types

| Type | Executable | Role |
|------|------------|------|
| `input` | no | Declares run-input keys; source for `inputs_map.from = "input"` |
| `task` | yes | Worker task (`task_id`); agent is the face, not a graph node |
| `batch_loop` | yes | For-each over a JSON array (`source_variable`, inner `task_ids`) |
| `hitl` | no (desugared) | Gate: marks `human_validation` on upstream task parents |
| `export` | no (desugared) | Folds into `expected_exports` + `export_instructions` |
| `loop` | yes | Structured for_each / while over a body; **frame** on canvas with output handle |
| `level` | no | Droppable column container; blocks inside get that `execution_level` |

Module: [`core/workflow_graph.py`](../core/workflow_graph.py)  
Canvas bridge: [`core/workflow_canvas.py`](../core/workflow_canvas.py)  
Canvas UI: [`ui/workflow_canvas_panel.py`](../ui/workflow_canvas_panel.py)  
Runtime: [`core/crew_builder.py`](../core/crew_builder.py)

### Data ports

```json
"inputs_map": {
  "brief": { "from": "node_abc", "key": "summary" },
  "query": { "from": "input", "key": "user_input" }
}
```

Task cards show **IN** (required_inputs / wires) and **OUT** (result / pydantic / HITL / export link).

### Structured loops (no impossible cycles)

```json
{
  "id": "loop_1",
  "type": "loop",
  "loop_mode": "while",
  "scope": "nodes",
  "body_node_ids": ["n1", "n2"],
  "max_iterations": 10,
  "exit_on_hitl": true,
  "exit_condition": {
    "output_match": {
      "source_node": "n2",
      "pattern": "\"approved\":\\s*true"
    }
  }
}
```

- `loop_mode`: `for_each` | `while`
- `scope`: `nodes` | `levels` | `workflow`
- **While exit:** stops when HITL in body approves **and/or** regex matches a body task output (max_iterations is safety cap)
- Body must be an internal DAG
- Outside nodes must depend on the **loop supernode**, not on body members
- Overlapping loop bodies are rejected

### Edit from workflow (write-through)

From the Canvas **Inspector** on a task card you may change globally (Task Builder asset):

- name, description, expected output
- specialization, tools, agent
- required inputs, human_validation (HITL flag)

**Locked in workflow:** delete task asset, change `model_id`.

Removing a **block** from the canvas only unlinks the node from the graph; it does not delete the shared task.

### Editor modes (Workflow Assembler)

1. **Canvas (drag & drop)** — Level columns (drop + auto-stack), resizable blocks, Loop-select frames (while/for on frame), inspector
2. **Lanes (classic)** — level columns + Graphviz mini-map

Install: `pip install streamlit-flow-component`

Edit lifecycle: **load → mutate draft → validate → canonicalize → create/update**.

---

## Phase B notes

- Alfredo JSON remains source of truth; the canvas only mutates the draft
- Save/Update stays in Streamlit
- Same `validate_graph` / `desugar_graph_for_runtime` for both editors
