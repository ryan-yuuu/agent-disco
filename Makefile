# Calfcord developer tasks.
#
# ``make dev`` brings up the full stack with hot-reload (docker compose
# watch), layering the committed dev overlay (docker-compose.dev.yml)
# onto the base compose file. A plain ``docker compose up`` (no make)
# uses only docker-compose.yml, which is production-shaped.

# Base + dev overlay, selected explicitly. The dev overlay is NOT named
# docker-compose.override.yml, so Compose does not auto-merge it — both
# files are passed with -f here.
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

.DEFAULT_GOAL := help
.PHONY: help dev down logs

# Show available targets (the default when you run a bare ``make``).
help:
	@echo "Calfcord dev tasks:"
	@echo "  make dev    Start the full stack with hot-reload (Ctrl-C to stop)"
	@echo "  make down   Stop and remove the dev stack"
	@echo "  make logs   Tail logs from the running dev stack"

# Start the full stack with hot-reload; Ctrl-C to stop.
dev:
	$(COMPOSE_DEV) watch

# Stop and remove the dev stack.
down:
	$(COMPOSE_DEV) down

# Tail logs from the running dev stack.
logs:
	$(COMPOSE_DEV) logs -f
