# Agora hub — convenience targets (issue #58).
# Quick start:  make up TOKEN=mysecret      (build + run the server in Docker)
#               make logs                    (follow logs)
#               make down                    (stop)
.PHONY: help up down logs build restart install serve test

TOKEN ?= changeme
PORT  ?= 8910
HUB_DIR ?= ./hub-data

help:                ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

up:                  ## Build + start the hub server in Docker (TOKEN=, PORT=, HUB_DIR=)
	AGENT_HUB_TOKEN=$(TOKEN) AGORA_PORT=$(PORT) AGENT_HUB_DIR=$(HUB_DIR) \
	  docker compose up -d --build
	@echo "Agora hub up → http://localhost:$(PORT)/   (token: $(TOKEN))"

down:                ## Stop the hub server
	docker compose down

logs:                ## Follow server logs
	docker compose logs -f

restart:             ## Restart the hub server
	docker compose restart

build:               ## Build the image only
	docker compose build

install:             ## Local (non-Docker) editable install
	pip install -e .

serve:               ## Run the server locally (no Docker)
	hubcli serve --host 127.0.0.1 --port $(PORT)

test:                ## Run the test suite
	@for f in tests/test_*.py; do python "$$f" || exit 1; done
