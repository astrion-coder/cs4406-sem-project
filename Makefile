LLVM_PREFIX ?= $(shell brew --prefix llvm)
OPENSSL_PREFIX ?= $(shell brew --prefix openssl@3)

export CC := $(LLVM_PREFIX)/bin/clang
export CXX := $(LLVM_PREFIX)/bin/clang++
export GOTOOLCHAIN := local
export CGO_ENABLED := 1

.SHELLFLAGS := -eu -c

# name,url,pinned commit.
# The separator must not be a shell metacharacter: these words are expanded unquoted in a for loop.
PINS := \
	simplepir,https://github.com/ahenzinger/simplepir,e9020b03bf2872c75b8954e749e32408b5db87ed \
	verisimplepir,https://github.com/leodec/verisimplepir,3643bb7cbaae02da98a195c4d004d4d083e3ab88 \
	apir-code,https://github.com/dedis/apir-code,35cabd62fb898926f5bea6035caeae39091e091d

.PHONY: setup patch build adapters test conformance recovery sysinfo smoke bench analyse figures netem session-check

setup:
	for pin in $(PINS); do \
		name=$${pin%%,*}; rest=$${pin#*,}; url=$${rest%%,*}; sha=$${rest#*,}; \
		dir=third_party/$$name; \
		if [ ! -e $$dir/.git ]; then \
			if [ -f .gitmodules ] && grep -q "path = $$dir\$$" .gitmodules; then \
				git submodule update --init $$dir; \
			else \
				git submodule add $$url $$dir; \
			fi; \
		fi; \
		git -C $$dir checkout --quiet $$sha; \
		git add $$dir; \
		echo "$$name at $$(git -C $$dir rev-parse HEAD)"; \
	done
	cd third_party/apir-code && go mod download
	uv sync

patch:
	for sub in patches/*/; do \
		[ -d "$$sub" ] || continue; \
		name=$$(basename $$sub); \
		for p in $$sub*.patch; do \
			[ -e "$$p" ] || continue; \
			if git -C third_party/$$name apply --reverse --check $(CURDIR)/$$p 2>/dev/null; then \
				echo "already applied: $$p"; \
			elif git -C third_party/$$name apply --check $(CURDIR)/$$p; then \
				git -C third_party/$$name apply $(CURDIR)/$$p; \
				echo "applied: $$p"; \
			else \
				echo "PATCH FAILED (upstream drift?): $$p neither applies nor reverse-applies to third_party/$$name" >&2; \
				exit 1; \
			fi; \
		done; \
	done

build: patch
	$(MAKE) -C third_party/verisimplepir LLVM_PREFIX=$(LLVM_PREFIX) OPENSSL_PREFIX=$(OPENSSL_PREFIX)
	cd third_party/simplepir/pir && go build ./...
	@# Library packages only: upstream's old go.sum lacks grpc and survey, which cmd/grpc, lib/proto and simulations need.
	cd third_party/apir-code && go build ./lib/client ./lib/server ./lib/database
	$(MAKE) adapters

ADAPTERS := build/adapters

adapters:
	mkdir -p $(ADAPTERS)
	cd src/adapters/simplepir_go && GOPROXY=off go build \
		-ldflags "-X main.upstreamSHA=$$(git -C $(CURDIR)/third_party/simplepir rev-parse HEAD)" \
		-o $(CURDIR)/$(ADAPTERS)/simplepir-adapter .
	cd src/adapters/apir_go && GOPROXY=off go build \
		-ldflags "-X main.upstreamSHA=$$(git -C $(CURDIR)/third_party/apir-code rev-parse HEAD)" \
		-o $(CURDIR)/$(ADAPTERS)/apir-adapter .
	$(MAKE) -C src/adapters/vsp_cpp LLVM_PREFIX=$(LLVM_PREFIX) OPENSSL_PREFIX=$(OPENSSL_PREFIX)

test:
	uv run python -m cs4406_sem_project.harness.upstream_tests $(if $(ONLY),--only $(ONLY))

conformance: adapters
	uv run python -m cs4406_sem_project.harness.conformance

recovery: adapters
	uv run python -m cs4406_sem_project.harness.conformance --suite recovery

sysinfo:
	uv run python -m cs4406_sem_project.harness.sysinfo --json env/sysinfo.json --platform-file env/reference_platform.txt

# RESUME=<run_id> continues a killed run; DRY=1 prints the plan and a time bound.
GRID_ARGS = $(if $(RESUME),--resume $(RESUME)) $(if $(DRY),--dry-run)

smoke: adapters sysinfo
	uv run python -m cs4406_sem_project.harness.run_grid --tier smoke $(GRID_ARGS)

bench: adapters sysinfo
	uv run python -m cs4406_sem_project.harness.run_grid --tier tier1 $(GRID_ARGS)

# RUNS="results/raw/<run_id> ..." names the runs to analyse. SYNTHETIC=<tier> builds the figures from
# synthetic rows instead, each marked "SYNTHETIC, not a result".
RUNS_ARGS = $(if $(SYNTHETIC),--synthetic $(SYNTHETIC),--runs $(or $(RUNS),$(error set RUNS="results/raw/<run_id> ..." or SYNTHETIC=<tier>)))

analyse:
	uv run python -m cs4406_sem_project.analysis.aggregate --runs $(or $(RUNS),$(error set RUNS="results/raw/<run_id> ...")) --out results/processed

# SESSION=results/raw/<run_id>/session adds F8 and NETEM=results/raw/<run_id>/netem adds F9; without them
# the two figures are skipped with a note.
figures:
	uv run python -m cs4406_sem_project.analysis.plots $(RUNS_ARGS) $(if $(SESSION),--session $(SESSION)) $(if $(NETEM),--netem $(NETEM)) --out results/figures

# RQ3b and RQ3a (task SG-6). Bring the link up first: sudo src/cs4406_sem_project/harness/dummynet.sh up <profile>
# then calibrate it (gate G4). PROFILE=none runs the loopback baseline. RUN_ID groups every profile of one run.
netem: adapters
	uv run python -m cs4406_sem_project.harness.netem --profile $(or $(PROFILE),$(error set PROFILE=lan|broadband|mobile|none)) --run-id $(or $(RUN_ID),$(error set RUN_ID=<p6 run id>))

session-check: adapters
	uv run python -m cs4406_sem_project.harness.session_check --grid-run $(or $(GRID_RUN),$(error set GRID_RUN=<grid run id>)) --run-id $(or $(RUN_ID),$(error set RUN_ID=<p6 run id>))

# RQ6 and RQ7 (task SG-7). The load generator uses only Go's standard library.
LOADGEN := build/loadgen/loadgen

.PHONY: loadgen concurrency

loadgen:
	mkdir -p $(dir $(LOADGEN))
	cd src/loadgen && GOPROXY=off go build -o $(CURDIR)/$(LOADGEN) .

# Every focus cell: the closed and open sweeps (RQ6) and the worker sweep (RQ7) from configs/load.yaml, then
# gate G7 on the rows. RUN_ID groups and resumes a run; DRY=1 prints the plan; EXPERIMENTS=closed,open,multicore.
concurrency: adapters loadgen sysinfo
	uv run python -m cs4406_sem_project.harness.loadgen --run-id $(or $(RUN_ID),$(error set RUN_ID=<p9 run id>)) $(if $(EXPERIMENTS),--experiments $(EXPERIMENTS)) $(if $(DRY),--dry-run)
	$(if $(DRY),,uv run python -m cs4406_sem_project.harness.loadgen --run-id $(RUN_ID) --g7)
