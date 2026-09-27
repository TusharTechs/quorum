# Quorum developer and operator shortcuts. Everything also works without make.
.PHONY: up down reset check check-ext test wheels logs shell backup restore demo-burst

up:            ## build and start the whole stack (seeded, demo mode)
	docker compose up --build -d && docker compose logs -f web | sed -n '/Quorum ready/,/====/p'

down:          ## stop the stack (data is kept in volumes)
	docker compose down

reset:         ## stop and DELETE all data (fresh seed on next `make up`)
	docker compose down -v

check:         ## run the official DOGFOOD acceptance checker
	python3 run.py .dogfood.toml | tee acceptance-report.txt

check-ext:     ## run the extended T3/T4 acceptance checker
	python3 tools/acceptance_ext.py .dogfood.toml | tee acceptance-report-extended.txt

test:          ## unit, API and engine tests (needs the dev database: docker compose -f docker-compose.dev.yml up -d)
	cd src && QUORUM_ENV=test DJANGO_DEBUG=1 ../.venv/bin/python -m pytest ../tests -q

wheels:        ## download dependency wheels for an air-gapped image build (linux, this machine's arch)
	pip download --only-binary=:all: --python-version 3.12 --implementation cp \
	  --platform manylinux2014_$$(docker info --format '{{.Architecture}}' | sed 's/amd64/x86_64/') \
	  --platform manylinux_2_28_$$(docker info --format '{{.Architecture}}' | sed 's/amd64/x86_64/') \
	  -r requirements.txt -d vendor/wheels

logs:
	docker compose logs -f web worker

shell:
	docker compose exec web python manage.py shell

backup:        ## database + uploaded files -> backups/<timestamp>/
	sh scripts/backup.sh

restore:       ## restore from a backup directory: make restore DIR=backups/2026-09-28T1200
	sh scripts/restore.sh $(DIR)

demo-burst:    ## simulate a suspicious burst of community votes (for the integrity demo)
	docker compose exec web python /app/scripts/simulate_vote_burst.py
