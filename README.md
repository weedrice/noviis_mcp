# noviis-mcp

NoviIs Agent MCP server implementation.

## Run

Development:

```bash
$env:NOVIIS_ENV="development"
python main.py
```

Production:

```bash
$env:NOVIIS_ENV="production"
python main.py
```

## Environment

The server always loads `.env`, then `.env.local`.

- `development`: missing values fall back to built-in local defaults
- `production`: all required values must be present in `.env` or `.env.local`

Use `.env.example` as the base template.
