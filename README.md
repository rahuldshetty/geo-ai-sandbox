# Geo-AI Harness

A geospatial-analysis agent in a custom web UI: a live
[GeoLibre](https://geolibre.app) map on the left, and a Jupyter/Colab-style
notebook panel on the right. Prompt an agent that drives the map and runs
GDAL/rasterio and GeoPandas work behind the scenes, or run raw Python directly.

## Setup

```bash
cd C:/Files/QGIS/geo-ai
.venv/Scripts/python.exe -m pip install -r requirements.txt   # or see pyproject.toml
```

Copy the environment template and fill in your provider key:

```bash
copy .env.example .env
```

`.env` settings (loaded automatically at startup):

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Provider API key (OpenAI / Azure / compatible endpoints). |
| `OPENAI_BASE_URL` | Optional custom OpenAI-compatible endpoint (Azure OpenAI, LiteLLM, vLLM, OpenRouter, local proxy). Leave empty for OpenAI's default. |
| `GEOAI_MODEL` | Model string. Default `openai:gpt-4o`. Also `anthropic:claude-sonnet-4-5`, `google-gla:gemini-2.5-pro`, `ollama:llama3.1`. |
| `GEOAI_WORKSPACE` | Optional default workspace name (overrides the notebook default). |

## Run

```bash
.venv/Scripts/python.exe -m geoai.server
```

## Usage

- **Map (left)** — the live GeoLibre map. Agent tool calls mutate it in place and
  it persists to `maps/current.geolibre.json` after every change.
- **File menu (top)** — `New` creates a workspace, `Open`/`Load` switches to an
  existing one, `Save` persists the current map project.
- **Cells tab** — a notebook-like prompt. Pick `Prompt` to send a message to the
  GeoAI agent (which runs the tools), or `Python` to execute a command directly in
  the kernel. Outputs accumulate as `In[n]` / `Out[n]` cells.
- **Data tab** — a tree explorer of the workspace files, with **Import files** and
  **Import folder** buttons that copy into the workspace's `data/` folder (plus a
  URL download field). Imported data is automatically included in the agent's
  context, so a later prompt can refer to it directly.

## Workspace layout

Each workspace lives at `workspaces/<name>/`:

```
data/     user inputs (imports, downloads, dropped files) — read here
results/  your outputs (GeoTIFF/COG, GeoJSON, tables) — write here
maps/     saved .geolibre.json projects
workspace.json   manifest (outputs + version)
```

## Notes

- `gdal_translate` (the osgeo escape hatch) requires the `gdal` package, which has
  no Windows wheels; all other raster tools are rasterio-based and work without it.
- `run_python` runs unsandboxed in the kernel process; the system prompt steers the
  agent toward the structured tools first.
