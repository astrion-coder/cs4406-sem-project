ROOT := $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../../..)
VSP := $(ROOT)/third_party/verisimplepir
OUT := $(ROOT)/build/adapters/vsp-adapter
SRC_DIR := $(ROOT)/src/adapters/vsp_cpp

LLVM_PREFIX ?= $(shell brew --prefix llvm)
OPENSSL_PREFIX ?= $(shell brew --prefix openssl@3)
UPSTREAM_SHA := $(shell git -C $(VSP) rev-parse HEAD)
# Extra compile and link flags, such as EXTRA_FLAGS="-fsanitize=thread -g" for the race check
# in docs/serve_protocol.md ("Threads"). Build such a binary to its own OUT.
EXTRA_FLAGS ?=

CXX := $(LLVM_PREFIX)/bin/clang++
# Same headers and link line as upstream's demo binaries, so the adapter shares the library's
# libc++ ABI. No -DNDEBUG: upstream's header asserts must stay live.
CXXFLAGS := -std=c++17 -O3 -Wall -Wno-deprecated-copy -Wno-deprecated-declarations -Wno-nontrivial-memcall \
	-I$(LLVM_PREFIX)/include -I$(LLVM_PREFIX)/include/c++/v1/ -I$(OPENSSL_PREFIX)/include \
	-I$(VSP)/src/lib -DUPSTREAM_SHA=\"$(UPSTREAM_SHA)\"
LDFLAGS := -L$(VSP)/bin/lib -lverisimplepir \
	-L$(LLVM_PREFIX)/lib -Wl,-rpath,$(LLVM_PREFIX)/lib -L$(OPENSSL_PREFIX)/lib -lssl -lcrypto \
	-Wl,-rpath,@executable_path/../../third_party/verisimplepir/bin/lib

.PHONY: all clean FORCE
all: $(OUT)

# The dylib's install name is the relative path bin/lib/libverisimplepir.dylib, which dyld
# resolves against the working directory. Rewriting this binary's reference to @rpath makes
# it load through the rpath above from any directory, with no DYLD_LIBRARY_PATH.
$(OUT): $(SRC_DIR)/vsp_adapter.cpp $(SRC_DIR)/json.h $(SRC_DIR)/wire.h $(VSP)/bin/lib/libverisimplepir.dylib $(OUT).sha
	$(CXX) $(CXXFLAGS) $(EXTRA_FLAGS) -o $@ $(SRC_DIR)/vsp_adapter.cpp $(LDFLAGS) $(EXTRA_FLAGS)
	install_name_tool -change bin/lib/libverisimplepir.dylib @rpath/libverisimplepir.dylib $@

# Touched only when the submodule moves, so the embedded upstream_sha is never stale.
$(OUT).sha: FORCE
	@mkdir -p $(dir $@)
	@echo $(UPSTREAM_SHA) | cmp -s - $@ || echo $(UPSTREAM_SHA) > $@
FORCE:

clean:
	rm -f $(OUT) $(OUT).sha
