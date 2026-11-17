# Local development commands. Run `make help` for the list.
PY := .venv/bin/python

.PHONY: help infra infra-stop migrate admin api worker web web-prod test demo e2e openapi docker docker-stop

help:
	@echo "make infra        start PostgreSQL (127.0.0.1:55432) and Redis (127.0.0.1:56379) in Docker"
	@echo "make migrate      create or upgrade the database schema (alembic upgrade head)"
	@echo "make admin        create the first administrator (asks for e-mail and password; safe to repeat)"
	@echo "make api          API on http://127.0.0.1:8010"
	@echo "make worker       background search worker"
	@echo "make web          web app in development mode on http://127.0.0.1:3010"
	@echo "make web-prod     production build of the web app, then serve it on http://127.0.0.1:3010"
	@echo "make test         backend tests (needs 'make infra')"
	@echo "make demo         demo API on http://127.0.0.1:8011 with synthetic data (no network, no AI cost)"
	@echo "make e2e          browser tests on a demo stack of their own (builds the web app; ports 8011 and 3011)"
	@echo "make openapi      re-export openapi.json and regenerate the web app's API types"
	@echo "make docker       whole stack in Docker (web app on http://localhost:3010)"
	@echo "make infra-stop   stop PostgreSQL and Redis (data is kept)"

infra:
	docker compose up -d postgres redis

infra-stop:
	docker compose stop postgres redis

migrate:
	.venv/bin/alembic upgrade head

admin:
	$(PY) -m app.portal.bootstrap

api:
	.venv/bin/uvicorn app.portal.app:app --host 127.0.0.1 --port 8010 --no-access-log

worker:
	.venv/bin/arq app.portal.worker.WorkerSettings

web:
	cd frontend && npm install && npm run dev

web-prod:
	cd frontend && npm install && npm run build && npm run start

test:
	$(PY) -m pytest -q

demo:
	$(PY) -m tests.e2e.server --reset

e2e:
	./tests/e2e/run.sh

openapi:
	$(PY) -m app.portal.export_openapi > openapi.json
	cd frontend && npm run gen:api

docker:
	docker compose --profile app up --build

docker-stop:
	docker compose --profile app down
