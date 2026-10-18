# Log and Order

Log and Order is still under active development and is not ready for production use yet.

In the software engineering system, production failures are investigated by two separate but equally important groups: the parsers, who uncover the evidence, and the local language models, who reconstruct the incidents. These are their reports.

DUN DUN. 🔨

===========================================================================================

Order a noisy log into one incident report, or summarize another text file.

A timestamped application log becomes a short incident report. The script extracts the events in code and decides which lines belong to the main failure, which lines are related, and which warnings stand alone. `LiquidAI/lfm2.5-2.6b` writes **Main failure** and **Related events** from that incident only. **Other warnings** is written in code from the remaining lines, so the same line is not listed twice. Line numbers in the report point back at the source file. The model does not suggest a fix. If a reply never leaves a thinking trace, it is retried once with a larger token budget. If that retry is still empty, the two narrative sections are filled from the extracted lines.

`--format agent` and `--format json` use the same extracted incident. They ask the local model for hypotheses, investigation steps, and regression scenarios, then drop any claim that cites a log line or a source name the extraction did not supply. `--no-llm` skips that call and still writes the full factual report. If Ollama cannot be reached or the reply fails validation, the factual report is written anyway.

Extraction keeps errors and the warnings that describe a real problem. Health checks, skipped work, not-found responses, and warnings that later recover are left out. A stack trace keeps the exception and the application frame. Request and response lines that share the error's trace stay with it, as do the config change, missing value, and failed setup lines just before the crash when they share a component, job, key, or other id. The worker exit, service failure, connection refusals, restart, and recovery that follow an exception stay with that exception. The same exception a few seconds apart is one incident. The same text hours later, or after the service has started again, is another. Repeated lines are counted once, with the first and last time.

A Jenkins console log is recognised from console markers such as `[Pipeline]`, `Started by`, and `Finished:`. Declarative and scripted pipelines keep their stage names, including nested stages. A freestyle job is read without stages. The parser works when the console has no timestamps and when the Timestamper plugin prefixed each line. It does not invent timestamps. The primary failure is the tool diagnostic, such as a compiler or test error. `Finished: FAILURE` and `script returned exit code` stay symptoms when a more specific line is present. Parallel output that does not name its branch is reported as an uncertain stage. Obvious secrets are redacted before they reach a report or the model. Line numbers still match the console file.

The human report names the build result, stage, category, command, and exit code when those were extracted, then the evidence lines. `--format agent` and `--format json` add a `jenkins` object to the same incident document. A successful Jenkins build does not invent a failure. A Jenkins log does not need a running Jenkins server.

Any other file is summarized with `LiquidAI/lfm2.5-2.6b` through [LangChain](https://python.langchain.com). Longer files are split into chunks, summarized one part at a time, and then combined. A chunk with nothing to report is skipped. A reply that never leaves a thinking trace is discarded and retried once.

## Requirements

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- [Ollama](https://ollama.com), running locally
- The `LiquidAI/lfm2.5-2.6b` model

## Setup

```bash
uv sync
ollama pull LiquidAI/lfm2.5-2.6b
```

## Usage

```bash
uv run main.py path/to/file
uv run main.py example-logs/jenkins_noisy_build_failure.log
uv run main.py example-logs/jenkins_typescript_failure.log --format agent --no-llm
uv run main.py example-logs/jenkins_pytest_failure.log --format json --output incident.json
uv run main.py path/to/file --format agent
uv run main.py path/to/file --format json --output incident.json
uv run main.py path/to/file --format agent --no-llm
uv run main.py path/to/file --format json --model LiquidAI/lfm2.5-2.6b
uv run main.py --dump-events path/to/file
```

`--format human` is the default. It asks the model for **Main failure** and **Related events**. `--format agent` writes a Markdown incident brief, including a task for a coding agent. `--format json` writes that same incident as JSON, with an `analysis` object. Both call Ollama unless `--no-llm` is set. `--model` selects a different local model. When `--model` is omitted, `LOG_AND_ORDER_MODEL` is used if that variable is set. `--ollama-url` selects the Ollama server. When it is omitted, `OLLAMA_BASE_URL` is used if that variable is set, and otherwise the client keeps its local default. `--output` writes the report to a file instead of stdout.

`--format agent` and `--format json` need a timestamped application log or a Jenkins console log. Another text file exits 1. `--no-llm` on a non-log file also exits 1, because that summary is a model call. A Jenkins log is detected automatically. No extra flag is required.

A timestamped log with no kept errors or warnings prints `No relevant errors or warnings were found.` for the human report. Agent and JSON still write a report that says no primary failure was identified.

`--dump-events` prints the incident grouping and the lines that would be sent to the model, and does not call Ollama. It ignores `--format`. A Jenkins console log is dumped the same way. A file that is neither a timestamped log nor a Jenkins console prints `Not a timestamped log.`

The summary is written to stdout. Progress messages go to stderr, so you can pipe the summary:

```bash
uv run main.py app.log > summary.txt
```

The file must be text. An empty file prints `The file is empty.` A file containing a null byte is rejected.

These logs in `example-logs/` are noisy incidents the report is meant to digest:

- `example-logs/spring_boot_incident_noise.log`
- `example-logs/django_rest_crash_noisy.log`
- `example-logs/gin_web_crash_noisy.log`
- `example-logs/actix_web_crash_noisy.log`
- `example-logs/nodejs_express_crash_noisy.log`
- `example-logs/aspnet_core_crash_noisy.log`

Jenkins consoles in `example-logs/` include a noisy TypeScript build failure, with and without Timestamper prefixes, plus smaller pipeline, freestyle, test, deploy, and infrastructure failures.

## MCP server

The same analysis is available to MCP clients such as Claude Code, Codex, and Cursor. The server is a read-only adapter. It does not parse logs itself, does not connect to a Jenkins server, and does not edit source files. A downloaded Jenkins console log is enough.

Deterministic analysis is the default. `use_llm` is false unless the client sets it, so the report does not call Ollama. Set `use_llm` true to add the same local enrichment the CLI uses for `--format json`. If Ollama is down or the reply fails validation, the factual report is still returned.

### Install and launch

MCP is an optional extra. People who only use the command line can leave it out.

```bash
uv sync --extra mcp
```

A normal `uv sync` also installs the SDK for development, because the dev dependency group depends on `mcp` too. `uv sync --no-dev --no-extra mcp` keeps the CLI install without the server.

The process speaks MCP on stdin and stdout and keeps running until the client disconnects. Diagnostics stay on stderr. In a terminal with no client it looks idle, which is expected.

```bash
LOG_AND_ORDER_ROOT=/absolute/path/to/logs \
  uv run --extra mcp python -m log_and_order.mcp_server
```

`LOG_AND_ORDER_ROOT` is the only directory the file tools will read. The path is resolved, including symlinks, and must already be a directory. Relative tool paths are joined to that directory. Absolute paths and symlinks that leave it are rejected. When the variable is unset, the process working directory is the root, and the server says so on stderr. Set the variable when a client starts the server from another directory.

Inputs larger than 32 MiB are rejected. `read_log_lines` returns at most 200 lines, and each returned line is capped at 2,000 characters. Obvious secrets are redacted with the same replacements the Jenkins parser uses. The server never runs commands found in a log.

### Tools

`analyse_log` reads one log file and returns the JSON incident document used by `--format json`.

- `path` (required): file inside `LOG_AND_ORDER_ROOT`
- `use_llm`: default `false`
- `model`: optional Ollama model name
- `detail`: `full` (default) or `compact`

`detail=full` is that document unchanged: incident, locations, timeline, evidence, analysis, other warnings, and `jenkins` when the file is a console log. `source_line` is a line in application code. Evidence line numbers are lines in the log. `detail=compact` keeps the incident, locations, Jenkins facts, and those line numbers, and drops log excerpts.

`read_log_lines` returns original log lines for the numbers in the report.

- `path` (required)
- `start_line` (required, 1-based)
- `count`: default 30, capped at 200

`analyse_text` analyses log text that is not stored in a file. Line numbers are relative to `content`. The result uses the same document. Nothing is written to disk.

- `content` (required)
- `use_llm`: default `false`
- `model`
- `detail`: `full` or `compact`

A coding agent that wants to change the application uses this evidence in its own session. The MCP server does not apply a fix.

### Claude Code

Project configuration in `.mcp.json`. Replace both absolute paths.

```json
{
  "mcpServers": {
    "log-and-order": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/log-reader",
        "--extra",
        "mcp",
        "python",
        "-m",
        "log_and_order.mcp_server"
      ],
      "env": {
        "LOG_AND_ORDER_ROOT": "/absolute/path/to/logs"
      }
    }
  }
}
```

Or add it from the shell:

```bash
claude mcp add \
  --env LOG_AND_ORDER_ROOT=/absolute/path/to/logs \
  --transport stdio \
  log-and-order -- \
  uv run --directory /absolute/path/to/log-reader --extra mcp python -m log_and_order.mcp_server
```

### Codex

`~/.codex/config.toml`, or `.codex/config.toml` for a trusted project:

```toml
[mcp_servers.log-and-order]
command = "uv"
args = [
  "run",
  "--directory",
  "/absolute/path/to/log-reader",
  "--extra",
  "mcp",
  "python",
  "-m",
  "log_and_order.mcp_server",
]

[mcp_servers.log-and-order.env]
LOG_AND_ORDER_ROOT = "/absolute/path/to/logs"
```

```bash
codex mcp add log-and-order \
  --env LOG_AND_ORDER_ROOT=/absolute/path/to/logs -- \
  uv run --directory /absolute/path/to/log-reader --extra mcp python -m log_and_order.mcp_server
```

### Cursor

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "log-and-order": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/absolute/path/to/log-reader",
        "--extra",
        "mcp",
        "python",
        "-m",
        "log_and_order.mcp_server"
      ],
      "env": {
        "LOG_AND_ORDER_ROOT": "/absolute/path/to/logs"
      }
    }
  }
}
```

### MCP Inspector

The Inspector starts the server the same way a client does. The environment variable is inherited by that process.

```bash
LOG_AND_ORDER_ROOT=/absolute/path/to/logs \
  npx @modelcontextprotocol/inspector \
  uv --directory /absolute/path/to/log-reader run --extra mcp python -m log_and_order.mcp_server
```

## Running Log and Order as an HTTP Server

The HTTP server is a third interface over the same analysis pipeline. The command line and the MCP server stay as they are. A Jenkins job, another CI system, or a monitoring tool can POST a log and receive the JSON incident document that `--format json` already writes. The server does not contain Ollama and does not download model weights. It calls an Ollama server you already run.

FastAPI is an optional extra, in the same way the MCP server is.

```bash
uv sync --extra api
OLLAMA_BASE_URL=http://127.0.0.1:11434 \
LOG_AND_ORDER_MODEL=LiquidAI/lfm2.5-2.6b \
uv run --extra api python -m log_and_order.api
```

The process listens on `127.0.0.1:8080` unless `LOG_AND_ORDER_HOST` and `LOG_AND_ORDER_PORT` say otherwise. It checks Ollama at startup and prints a warning if the server or the model is missing. That warning does not stop the process. `GET /api/v1/health` answers while Ollama is down. `GET /api/v1/ready` returns 503 until the configured server lists the configured model.

### Docker

```bash
export OLLAMA_BASE_URL=http://192.168.1.100:11434
export LOG_AND_ORDER_MODEL=LiquidAI/lfm2.5-2.6b
docker compose up -d --build
```

Compose refuses to start when either variable is missing. The image runs Uvicorn as a non-root user, publishes `127.0.0.1:8080` on the host, and restarts after an unexpected exit. The container has no GPU and no Ollama installation. Set `LOG_AND_ORDER_API_KEY` before you publish the port on any interface other than localhost.

Copy [`.env.example`](.env.example) to `.env` if you want Compose to read the variables from a file. Do not commit a real API key.

| Variable | Role |
| --- | --- |
| `OLLAMA_BASE_URL` | Required. Full Ollama URL, including the port. Used as given. |
| `LOG_AND_ORDER_MODEL` | Required. Model name that is already installed on that server. |
| `LOG_AND_ORDER_API_KEY` | Optional Bearer token. Empty means open access, which is only suitable on localhost. |
| `LOG_AND_ORDER_MAX_UPLOAD_MB` | Largest accepted upload. Default 32. |
| `LOG_AND_ORDER_OLLAMA_TIMEOUT` | Inference timeout in seconds. Default 120. |
| `LOG_AND_ORDER_MAX_CONCURRENT_ANALYSES` | How many model calls may run at once. Default 1. |
| `LOG_AND_ORDER_PORT` | Port inside the container. Default 8080. |
| `LOG_AND_ORDER_PUBLISH_PORT` | Localhost port on the host. Default 8080. |

The HTTP port and Ollama's port 11434 are independent. The container does not rewrite the Ollama URL and does not search the network for a server.

### Where Ollama runs

`127.0.0.1` inside an ordinary bridge container is the container, not the machine running Docker.

- **Ollama on another machine.** Set `OLLAMA_BASE_URL` to that machine, for example `http://192.168.1.100:11434`. The container uses the address you supply.
- **Ollama in another container.** Put both containers on one Docker network and use the Ollama service name, for example `http://ollama:11434`. Ollama must already be running. This image will not start it or pull a model.
- **Ollama on the same Linux host, listening on all interfaces.** Start Ollama with `OLLAMA_HOST=0.0.0.0:11434` and set `OLLAMA_BASE_URL=http://host.docker.internal:11434`. Compose adds `host.docker.internal` to the container. This does not work when Ollama listens only on `127.0.0.1`, because the host's loopback is not the Docker bridge.
- **Ollama on the same Linux host, listening only on loopback.** Host networking is the fit. `127.0.0.1` then means the host. Add `network_mode: host` to the service and remove the `ports` block, because host networking ignores port publishing. The API is then on the host's port 8080, so set `LOG_AND_ORDER_API_KEY` and keep that port off untrusted networks. Use `OLLAMA_BASE_URL=http://127.0.0.1:11434`.

### Submit a log

```bash
curl -sS -X POST http://127.0.0.1:8080/api/v1/analyze \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "file=@example-logs/jenkins_noisy_build_failure.log" \
  -o diagnosis.json
```

Omit the Authorization header when no API key is configured. The response is the existing JSON incident document. `analysis.enriched` is true when the model analysis was accepted. `analysis.fallback` is true, and `analysis.reason` explains why, when the report is the deterministic one. A fallback is still HTTP 200: the facts were extracted, and the model did not diagnose the incident. `analysis.failure_mechanism` is null in that case.

`GET /api/v1/models` lists the names Ollama already has. It cannot pull, delete, or switch models. With an API key set, this route and `POST /api/v1/analyze` require the Bearer token. Health and ready stay open and do not reveal the key or the Ollama URL.

A file that is empty, binary, or not UTF-8 is rejected. A file that is neither a Jenkins console nor a timestamped log returns 422. An upload past the size limit returns 413. The server reads the upload in memory, up to that limit, and does not keep a copy.

### Jenkins

Save the failing command's output to a known file, then upload that file from `post { failure }`. The diagnostic call is inside a catch so a down Log and Order service leaves the original build result unchanged. The API key comes from a Jenkins secret text credential, not from the Jenkinsfile.

```groovy
pipeline {
  agent any
  environment {
    LOG_AND_ORDER_URL = 'http://127.0.0.1:8080'
  }
  stages {
    stage('Test') {
      steps {
        sh '''
          set -o pipefail
          mkdir -p build
          ./run-tests.sh 2>&1 | tee build/failure.log
        '''
      }
    }
  }
  post {
    failure {
      withCredentials([string(credentialsId: 'log-and-order-api-key', variable: 'LOG_AND_ORDER_TOKEN')]) {
        script {
          try {
            if (!fileExists('build/failure.log')) {
              echo 'No build/failure.log to upload. The build result is unchanged.'
            } else {
              sh '''
                curl --fail-with-body --silent --show-error --max-time 180 \
                  -H "Authorization: Bearer ${LOG_AND_ORDER_TOKEN}" \
                  -F "file=@build/failure.log" \
                  "${LOG_AND_ORDER_URL}/api/v1/analyze" \
                  -o diagnosis.json
              '''
            }
          } catch (err) {
            echo "Log and Order did not return a diagnosis: ${err}"
          }
        }
      }
      archiveArtifacts artifacts: 'diagnosis.json', allowEmptyArchive: true
    }
  }
}
```

`tee` captures the command that failed. It does not capture every Jenkins wrapper line. When the agent is allowed to read its own build console, this alternative writes that console and uploads it. During `post`, the console can still be incomplete because the handler itself has not finished.

```groovy
withCredentials([
  string(credentialsId: 'log-and-order-api-key', variable: 'LOG_AND_ORDER_TOKEN'),
  usernamePassword(credentialsId: 'jenkins-api', usernameVariable: 'JENKINS_USER', passwordVariable: 'JENKINS_TOKEN'),
]) {
  sh '''
    curl --fail --silent --show-error --user "${JENKINS_USER}:${JENKINS_TOKEN}" \
      "${BUILD_URL}consoleText" -o build/console.log
    curl --fail-with-body --silent --show-error --max-time 180 \
      -H "Authorization: Bearer ${LOG_AND_ORDER_TOKEN}" \
      -F "file=@build/console.log" \
      "${LOG_AND_ORDER_URL}/api/v1/analyze" \
      -o diagnosis.json
  '''
}
```

Keep that block in the same `try` so a refused console download or a refused diagnosis does not replace the original failure.

### Authentication and exposure

Leave the Compose port binding at `127.0.0.1` when the only client is on the same machine. A Jenkins controller on another machine needs a published port or a private network path. Set `LOG_AND_ORDER_API_KEY` first, store that value in Jenkins as a secret text credential, and terminate HTTPS at a reverse proxy. The application speaks HTTP and does not terminate TLS itself. Do not put the key in the Jenkinsfile, the image, or the log.

The Ollama URL and the model name come from the server environment. A request cannot point the analysis at a different HTTP service. Uploaded logs are untrusted, including text that tries to instruct the model. The existing redaction and evidence checks still apply.

### Troubleshooting

`GET /api/v1/health` returns 200 when the process is up, even if `/api/v1/ready` returns 503.

- `reason` is `unreachable`: the process cannot open `OLLAMA_BASE_URL`. From the host, `curl -sS "$OLLAMA_BASE_URL/api/tags"` should return JSON. From a bridge container, `127.0.0.1` is the wrong address for a host-local Ollama. See the networking cases above.
- `reason` is `model_missing`: Ollama answered, and the configured name is not in `/api/tags`. Names often look like `LiquidAI/lfm2.5-2.6b:latest`. The server accepts that tag for the name `LiquidAI/lfm2.5-2.6b`. It will not pull a model and it will not substitute another one.
- The analysis returns 200 with `analysis.fallback` true and `analysis.reason` `unavailable`: extraction finished, and the model call did not. The failure mechanism and hypotheses are empty on purpose. A timeout uses `LOG_AND_ORDER_OLLAMA_TIMEOUT`.
- `analysis.enriched` true means the model reply passed the evidence checks. That is model analysis, not a confirmed cause.

## Tests

Extraction and report enrichment are checked without the model:

```bash
uv run python -m unittest
```

## License

[MIT](LICENSE)
