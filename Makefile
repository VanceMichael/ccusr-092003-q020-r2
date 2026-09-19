
.PHONY: migrate test run
migrate:
	python -m scripts.migrate
test:
	python -m unittest discover -s tests
run:
	python -m app.main
