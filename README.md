# PhotoMe development container

The container includes Python 3.12, Node.js 22, and the Claude Code CLI.

## Open automatically in VS Code

Install the **Dev Containers** extension (`ms-vscode-remote.remote-containers`).
When you open this folder in VS Code, it will automatically build and start the
container and reopen the workspace inside it. Open a VS Code terminal and run
`claude` to start Claude Code.

## Run manually

Build the image and start Claude Code in the mounted project directory:

```sh
docker compose build
docker compose run --rm photome
```

On first run, authenticate with Claude Code inside the container. Its
configuration is kept in the `claude-config` Docker volume across runs.

Run Python commands in the same project directory with:

```sh
docker compose run --rm photome python --version
```
