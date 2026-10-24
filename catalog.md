| harness | config file | format |
| --- | --- | --- |
| opencode | ${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.jsonc | opencode-jsonc |
| pi | $HOME/.pi/agent/models.json | pi-models |
| pig | $HOME/.pig/agent/models.json | pi-models |

# Provider catalog targets for refetch-catalog.py.
# Columns: harness | config file | format
# Formats: opencode-jsonc | pi-models
# Paths may use shell expansions ($HOME, ${XDG_CONFIG_HOME:-$HOME/.config}), evaluated at run time.
# Provider id, base URL, and API key are read from the environment:
#   NUTARALINE_API_KEY / GUTSAI_API_KEY, exported by ~/.config/agent-env.sh.