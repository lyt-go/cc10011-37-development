.PHONY: install backend frontend test typecheck

install:
	cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
	cd frontend && npm install

backend:
	cd backend && ./run.sh

frontend:
	cd frontend && npm run dev

test:
	cd backend && .venv/bin/python -m unittest discover -s tests

typecheck:
	cd frontend && npm run typecheck
