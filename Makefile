LLVM_PREFIX ?= $(shell brew --prefix llvm)
OPENSSL_PREFIX ?= $(shell brew --prefix openssl@3)

export CC := $(LLVM_PREFIX)/bin/clang
export CXX := $(LLVM_PREFIX)/bin/clang++
export GOTOOLCHAIN := local
export CGO_ENABLED := 1

.SHELLFLAGS := -eu -c

# name,url,pinned commit (dpf-go is the commit in apir-code's go.mod pseudo-version).
# The separator must not be a shell metacharacter: these words are expanded unquoted in a for loop.
PINS := \
	simplepir,https://github.com/ahenzinger/simplepir,e9020b03bf2872c75b8954e749e32408b5db87ed \
	verisimplepir,https://github.com/leodec/verisimplepir,3643bb7cbaae02da98a195c4d004d4d083e3ab88 \
	apir-code,https://github.com/dedis/apir-code,7a7f2041bc1e0907ef089f8e8e55252ef7f18e0f \
	dpf-go,https://github.com/dkales/dpf-go,6eae873488482fc6f01307d55f74b33862de6840

.PHONY: setup patch build test sysinfo

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
	cd third_party/apir-code && go build ./...

test:
	uv run python -m cs4406_sem_project.harness.upstream_tests $(if $(ONLY),--only $(ONLY))

sysinfo:
	uv run python -m cs4406_sem_project.harness.sysinfo --json env/sysinfo.json --platform-file env/reference_platform.txt
