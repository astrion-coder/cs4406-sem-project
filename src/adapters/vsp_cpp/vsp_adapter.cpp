// VeriSimplePIR adapter: arms S0-cpp, S1 and S2 (src/adapters/README.md).

#include "pir/preproc_pir.h"

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <new>
#include <openssl/evp.h>
#include <pthread.h>
#include <thread>
#include <type_traits>
#include <stdexcept>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#include "json.h"
#include "wire.h"

#ifndef UPSTREAM_SHA
#error "build with -DUPSTREAM_SHA=\"<verisimplepir commit>\""
#endif

namespace {

// ---------------------------------------------------------------- domain tables

enum class Phase { Offline, Online };

enum FaultKind : unsigned {
    kNone = 1,
    kFlipAnswerBit = 2,
    kWrongRecord = 4,
    kFlipHintBit = 8,
    kStaleRecord = 16,
    kDropServer = 32,
    kBadProof = 64,
};

struct FaultSpec {
    const char* name;
    FaultKind kind;
    bool offline, online;  // the phases whose repetitions it corrupts
    bool in(Phase p) const { return p == Phase::Offline ? offline : online; }
};

constexpr FaultSpec kFaults[] = {
    {"none", kNone, false, false},
    {"flip_answer_bit", kFlipAnswerBit, false, true},
    {"wrong_record", kWrongRecord, false, true},
    {"flip_hint_bit", kFlipHintBit, true, true},
    {"stale_record", kStaleRecord, false, true},
    {"drop_server", kDropServer, false, true},
    {"bad_proof", kBadProof, true, false},
};

struct ArmSpec {
    const char* name;
    bool simple_pir, preproc, honest_hint;  // VeriSimplePIR constructor flags
    unsigned faults;                        // FaultKind mask
    bool verifiable() const { return preproc; }
};

constexpr unsigned kAllFaults = kNone | kFlipAnswerBit | kWrongRecord | kFlipHintBit | kStaleRecord | kDropServer | kBadProof;
constexpr ArmSpec kArms[] = {
    {"S0-cpp", true, false, false, kAllFaults & ~kBadProof},
    {"S1", false, true, true, kAllFaults},
    {"S2", false, true, false, kAllFaults},
};

// What the client does after it rejects an answer. retry_same is never allowed (decision 12),
// and every policy but abort needs an arm whose client can reject.
enum class Policy { Abort, SwitchReplica, RePreprocess };

struct PolicySpec {
    const char* name;
    Policy kind;
};

constexpr PolicySpec kPolicies[] = {
    {"abort", Policy::Abort},
    {"switch_replica", Policy::SwitchReplica},
    {"re_preprocess", Policy::RePreprocess},
};
constexpr uint64_t kRecoveryBudget = 3;  // further attempts after the first rejection

constexpr bool kAllowTrivial = true;  // as full_preproc_pir_test (pir_test.cpp:250)
constexpr const char* kAnswerKernel = "packed_basis26";
constexpr uint64_t kEntryBits = 8;     // a database entry is one byte of a record
constexpr uint64_t kMaxPadding = 64;   // largest library database, in entries, per database byte

// ---------------------------------------------------------------- options

// Bench mode is the default; serve and client mode follow docs/serve_protocol.md.
enum class Mode : unsigned { Bench = 1, Serve = 2, Client = 4 };

struct ModeSpec {
    const char* name;
    Mode mode;
};

constexpr ModeSpec kModes[] = {{"bench", Mode::Bench}, {"serve", Mode::Serve}, {"client", Mode::Client}};

constexpr unsigned kBench = unsigned(Mode::Bench), kServe = unsigned(Mode::Serve), kClient = unsigned(Mode::Client);

// Which modes take each flag, and which require it.
struct FlagSpec {
    const char* name;
    bool takes_value;
    unsigned modes, required_in;
};

constexpr FlagSpec kFlags[] = {
    {"--mode", true, kServe | kClient, kServe | kClient},
    {"--arm", true, kBench | kServe | kClient, kBench | kServe | kClient},
    {"--db-bytes", true, kBench | kServe | kClient, kBench | kServe | kClient},
    {"--record-bytes", true, kBench | kServe | kClient, kBench | kServe | kClient},
    {"--phase", true, kBench, kBench},
    {"--reps", true, kBench, kBench},
    {"--warmup", true, kBench, kBench},
    {"--seed", true, kBench | kServe | kClient, kBench | kServe | kClient},
    {"--fault", true, kBench, 0},
    {"--fault-prob", true, kBench, 0},
    {"--recovery", true, kBench, 0},
    {"--replicas-cheat", false, kBench, 0},
    {"--offline-in-process", false, kBench, 0},
    {"--workers", true, kServe, kServe},
    {"--listen", true, kServe, kServe},
    {"--server-id", true, kServe, 0},
    {"--connect", true, kClient, kClient},
    {"--session-queries", true, kClient, kClient},
    {"--corpus-out", true, kClient, 0},
    {"--out", true, kBench | kServe | kClient, kBench | kClient},
};

constexpr uint64_t kMaxWorkers = 256;

struct Options {
    Mode mode = Mode::Bench;
    const ArmSpec* arm = nullptr;
    uint64_t db_bytes = 0, record_bytes = 0;
    bool offline = false, online = false;
    uint64_t reps = 0, warmup = 0, seed = 0;  // client mode: reps is --session-queries
    const FaultSpec* fault = &kFaults[0];
    double fault_prob = 0.0;
    const PolicySpec* recovery = &kPolicies[0];
    bool replicas_cheat = false;
    bool offline_in_process = false;
    std::string out;
    uint64_t workers = 0;                 // serve
    std::optional<wire::Endpoint> listen;  // serve
    std::optional<wire::Endpoint> connect;  // client
    std::string corpus_out;               // client
};

constexpr const char* kUsage =
    "usage: vsp-adapter --arm S0-cpp|S1|S2 --db-bytes N --record-bytes N\n"
    "                   --phase offline|online|all --reps N --warmup N --seed N\n"
    "                   --fault none|flip_answer_bit|wrong_record|flip_hint_bit|\n"
    "                           stale_record|drop_server|bad_proof\n"
    "                   [--fault-prob F] [--recovery abort|switch_replica|re_preprocess]\n"
    "                   [--replicas-cheat] [--offline-in-process] --out PATH.jsonl\n"
    "       vsp-adapter --mode serve --arm ARM --db-bytes N --record-bytes N --seed N\n"
    "                   --workers W --listen HOST:PORT [--server-id 0] [--out PATH.jsonl]\n"
    "       vsp-adapter --mode client --arm ARM --db-bytes N --record-bytes N --seed N\n"
    "                   --connect HOST:PORT --session-queries Q --out PATH.jsonl\n"
    "                   [--corpus-out PATH]\n"
    "\n"
    "Drives VeriSimplePIR (third_party/verisimplepir) and appends one JSON record per\n"
    "repetition and phase to --out. Contract: src/adapters/README.md. Serve and client\n"
    "mode: docs/serve_protocol.md.\n"
    "Exit codes: 0 records written, 1 internal failure, 2 usage error.\n";

struct UsageError : std::runtime_error {
    using std::runtime_error::runtime_error;
};

uint64_t parse_u64(const std::string& flag, const std::string& v) {
    if (v.empty() || v[0] == '-') throw UsageError(flag + " needs a non-negative integer, got '" + v + "'");
    char* end = nullptr;
    errno = 0;
    unsigned long long x = std::strtoull(v.c_str(), &end, 10);
    if (errno || *end) throw UsageError(flag + " needs a non-negative integer, got '" + v + "'");
    return x;
}

wire::Endpoint parse_endpoint(const std::string& flag, const std::string& v) {
    const auto e = wire::parse_endpoint(v);
    if (!e) throw UsageError(flag + " needs HOST:PORT, got '" + v + "'");
    return *e;
}

Options parse_args(int argc, char** argv) {
    std::map<std::string, std::string> given;  // flag to value, "" for --replicas-cheat
    for (int i = 1; i < argc; i++) {
        const std::string flag = argv[i];
        const FlagSpec* spec = nullptr;
        for (const auto& f : kFlags)
            if (flag == f.name) spec = &f;
        if (!spec) throw UsageError("unknown flag " + flag);
        if (spec->takes_value && i + 1 >= argc) throw UsageError(flag + " needs a value");
        given[flag] = spec->takes_value ? argv[++i] : "";
    }

    Options o;
    const ModeSpec* mode = &kModes[0];
    if (given.count("--mode")) {
        mode = nullptr;
        for (const auto& m : kModes)
            if (given["--mode"] == m.name && m.mode != Mode::Bench) mode = &m;
        if (!mode) throw UsageError("--mode must be serve or client, not '" + given["--mode"] + "'");
    }
    o.mode = mode->mode;
    std::string missing;
    for (const auto& f : kFlags) {
        if (given.count(f.name) && !(f.modes & unsigned(o.mode))) {
            if (std::string(f.name) == "--fault")
                throw UsageError("--fault is a bench-mode flag: a targeted fault needs the queried index, which a "
                                 "PIR server does not know (docs/serve_protocol.md)");
            throw UsageError(std::string(f.name) + " is not a " + mode->name + "-mode flag");
        }
        if (!given.count(f.name) && (f.required_in & unsigned(o.mode))) missing += std::string(missing.empty() ? "" : ", ") + f.name;
    }
    if (!missing.empty()) throw UsageError(std::string(mode->name) + " mode requires " + missing);

    bool have_prob = false;
    for (const auto& [flag, v] : given) {
        if (flag == "--arm") {
            for (const auto& a : kArms)
                if (v == a.name) o.arm = &a;
            if (!o.arm) throw UsageError("this adapter serves S0-cpp, S1 and S2, not '" + v + "'");
        } else if (flag == "--db-bytes") {
            o.db_bytes = parse_u64(flag, v);
        } else if (flag == "--record-bytes") {
            o.record_bytes = parse_u64(flag, v);
        } else if (flag == "--phase") {
            o.offline = v == "offline" || v == "all";
            o.online = v == "online" || v == "all";
            if (!o.offline && !o.online) throw UsageError("--phase must be offline, online or all");
        } else if (flag == "--reps") {
            o.reps = parse_u64(flag, v);
        } else if (flag == "--warmup") {
            o.warmup = parse_u64(flag, v);
        } else if (flag == "--seed") {
            o.seed = parse_u64(flag, v);
        } else if (flag == "--fault") {
            o.fault = nullptr;
            for (const auto& f : kFaults)
                if (v == f.name) o.fault = &f;
            if (!o.fault) throw UsageError("unknown --fault '" + v + "'");
        } else if (flag == "--fault-prob") {
            char* end = nullptr;
            o.fault_prob = std::strtod(v.c_str(), &end);
            if (*end || v.empty() || !(o.fault_prob >= 0.0 && o.fault_prob <= 1.0))
                throw UsageError("--fault-prob must be a number in [0, 1]");
            have_prob = true;
        } else if (flag == "--recovery") {
            o.recovery = nullptr;
            for (const auto& p : kPolicies)
                if (v == p.name) o.recovery = &p;
            if (!o.recovery)
                throw UsageError("--recovery must be abort, switch_replica or re_preprocess, not '" + v +
                                 "' (retry_same is never allowed)");
        } else if (flag == "--replicas-cheat") {
            o.replicas_cheat = true;
        } else if (flag == "--offline-in-process") {
            o.offline_in_process = true;
        } else if (flag == "--out") {
            o.out = v;
        } else if (flag == "--workers") {
            o.workers = parse_u64(flag, v);
            if (o.workers == 0 || o.workers > kMaxWorkers)
                throw UsageError("--workers must be between 1 and " + std::to_string(kMaxWorkers));
        } else if (flag == "--listen") {
            o.listen = parse_endpoint(flag, v);
        } else if (flag == "--server-id") {
            if (parse_u64(flag, v) != 0) throw UsageError("S0-cpp, S1 and S2 have one server, so --server-id must be 0");
        } else if (flag == "--connect") {
            if (v.find(',') != std::string::npos)
                throw UsageError("S0-cpp, S1 and S2 have one server, so --connect takes one HOST:PORT");
            o.connect = parse_endpoint(flag, v);
        } else if (flag == "--session-queries") {
            o.reps = parse_u64(flag, v);
            if (o.reps == 0) throw UsageError("--session-queries must be positive");
        } else if (flag == "--corpus-out") {
            o.corpus_out = v;
        }
    }
    if (given.count("--out") && o.out.empty()) throw UsageError("--out needs a path");
    if (o.record_bytes == 0) throw UsageError("--record-bytes must be positive for " + std::string(o.arm->name));
    if (o.db_bytes == 0 || o.db_bytes % o.record_bytes)
        throw UsageError("--db-bytes must be a positive multiple of --record-bytes");
    if (!(o.arm->faults & o.fault->kind))
        throw UsageError(std::string(o.arm->name) + " does not support --fault " + o.fault->name);
    if (o.recovery->kind != Policy::Abort && !o.arm->verifiable())
        throw UsageError(std::string(o.arm->name) + " never rejects an answer, so it supports only --recovery abort");
    if (o.offline_in_process && o.fault->kind != kNone)
        throw UsageError("--offline-in-process takes --fault none: a faulted offline phase can end in an upstream "
                         "assert, which only a child may hit");
    if (o.fault->kind == kWrongRecord && o.db_bytes / o.record_bytes < 2)
        throw UsageError("wrong_record needs at least two records");
    if (!have_prob) o.fault_prob = o.fault->kind == kNone ? 0.0 : 1.0;
    // A client's queries are online records of a fault-free run, with no warm-up.
    if (o.mode == Mode::Client) o.online = true;
    return o;
}

// ---------------------------------------------------------------- measurement

struct Span {
    double wall = 0, cpu = 0;
    Span& operator+=(const Span& s) {
        wall += s.wall;
        cpu += s.cpu;
        return *this;
    }
};

double clock_s(clockid_t c) {
    timespec ts;
    clock_gettime(c, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
}

template <class F>
void timed(Span& acc, F&& body) {
    const double w0 = clock_s(CLOCK_MONOTONIC), c0 = clock_s(CLOCK_PROCESS_CPUTIME_ID);
    body();
    acc.wall += clock_s(CLOCK_MONOTONIC) - w0;
    acc.cpu += clock_s(CLOCK_PROCESS_CPUTIME_ID) - c0;
}

uint64_t peak_rss_bytes() {
    rusage ru;
    getrusage(RUSAGE_SELF, &ru);
#ifdef __APPLE__
    return ru.ru_maxrss;
#else
    return ru.ru_maxrss * 1024ULL;
#endif
}

// Client memory: the library's own arrays, 8 bytes per Elem limb and 1 per bool.
uint64_t memory_bytes(const Matrix& m) { return m.rows * m.cols * sizeof(Elem); }
uint64_t memory_bytes(const BinaryMatrix& m) { return m.rows * m.cols * sizeof(bool); }

// Wire sizes (src/adapters/README.md, "Encoding bytes"). Upstream has no serialiser, so a
// message is its elements, each in the bits its modulus needs, packed tightly into whole bytes.
uint64_t bits_below(uint64_t bound) { return 64 - __builtin_clzll(bound - 1); }  // bits of [0, bound)

struct WireBits {
    uint64_t q;           // Z_q, q = 2^logq: A, H, ct, ans
    uint64_t multi_limb;  // Z_{q kappa}, stored as two 64-bit limbs: A_2, H_2, preprocessing cts
    uint64_t proof;       // [0, p m), PreprocVerify's norm bound: the preprocessing proof
};

WireBits wire_bits(const VeriSimplePIR& pir) {
    return {LHE::logq, LHE::logq + bits_below(pir.preproc_lhe.kappa), bits_below(pir.dbParams.p * pir.m)};
}

uint64_t wire_bytes(uint64_t elems, uint64_t bits) { return (elems * bits + 7) / 8; }
uint64_t wire_bytes(const Matrix& m, uint64_t bits) { return wire_bytes(m.rows * m.cols, bits); }
uint64_t wire_bytes(const Multi_Limb_Matrix& m, uint64_t bits) { return wire_bytes(m.rows * m.cols, bits); }
uint64_t wire_bytes(const std::vector<Multi_Limb_Matrix>& v, uint64_t bits) {
    uint64_t elems = 0;
    for (const auto& m : v) elems += m.rows * m.cols;
    return wire_bytes(elems, bits);
}

// Upstream Matrix has no destructor (mat.h), so the adapter frees what it holds.
void release(Matrix& m) {
    free(m.data);
    m.data = nullptr;
}
void release(Multi_Limb_Matrix& m) {
    release(m.q_data);
    release(m.kappa_data);
}
void release(std::vector<Multi_Limb_Matrix>& v) {
    for (auto& m : v) release(m);
    v.clear();
}

// ---------------------------------------------------------------- isolation

constexpr int kAssertExit = 134;

// The clock of checks running in a child, in a MAP_SHARED page the parent reads afterwards.
// The child's SIGABRT handler stops it, so a rejection still has a span.
struct ChildClock {
    double wall0, cpu0;
    Span span;
    void start() {
        wall0 = clock_s(CLOCK_MONOTONIC);
        cpu0 = clock_s(CLOCK_PROCESS_CPUTIME_ID);
    }
    void stop() { span = {clock_s(CLOCK_MONOTONIC) - wall0, clock_s(CLOCK_PROCESS_CPUTIME_ID) - cpu0}; }
};
ChildClock* g_child_clock = nullptr;

extern "C" void exit_on_abort(int) {
    if (g_child_clock) g_child_clock->stop();
    _exit(kAssertExit);
}

struct Isolated {
    bool aborted;
    std::string output;  // the child's stdout and stderr: upstream's message and the assert line
};

// Runs body in a forked child, so an upstream assert ends only the child.
template <class F>
Isolated run_isolated(F&& body) {
    std::fflush(nullptr);
    int fds[2];
    if (pipe(fds)) throw std::runtime_error("pipe failed");
    const pid_t pid = fork();
    if (pid < 0) throw std::runtime_error("fork failed");
    if (pid == 0) {
        close(fds[0]);
        dup2(fds[1], STDOUT_FILENO);
        dup2(fds[1], STDERR_FILENO);
        close(fds[1]);
        setvbuf(stdout, nullptr, _IONBF, 0);
        std::signal(SIGABRT, exit_on_abort);
        try {
            body();
        } catch (...) {
            _exit(1);
        }
        _exit(0);
    }
    close(fds[1]);
    std::string out;
    char buf[4096];
    for (;;) {
        const ssize_t k = read(fds[0], buf, sizeof buf);
        if (k > 0) out.append(buf, k);
        else if (k == 0 || errno != EINTR) break;
    }
    close(fds[0]);
    int status = 0;
    while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    while (!out.empty() && (out.back() == '\n' || out.back() == ' ')) out.pop_back();
    if (WIFEXITED(status) && WEXITSTATUS(status) == 0) return {false, out};
    if (WIFEXITED(status) && WEXITSTATUS(status) == kAssertExit) return {true, out};
    throw std::runtime_error("isolated child failed (status " + std::to_string(status) + "): " + out);
}

// run_isolated for a body that produces a value. The child writes it into a shared mapping,
// so T must be trivially copyable. Empty when the child aborted, with its output as the reason.
template <class T, class F>
std::pair<std::optional<T>, std::string> run_isolated_value(F&& body) {
    static_assert(std::is_trivially_copyable_v<T>);
    void* shared = mmap(nullptr, sizeof(T), PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0);
    if (shared == MAP_FAILED) throw std::runtime_error("mmap failed");
    T* slot = new (shared) T{};
    Isolated r = run_isolated([&] { *slot = body(); });
    std::optional<T> value = r.aborted ? std::nullopt : std::optional<T>(*slot);
    munmap(shared, sizeof(T));
    return {value, std::move(r.output)};
}

struct Verdict {
    bool rejected;
    Span span;  // the checks, up to the rejection when there was one
};

// Checks that may reject, run in a child: upstream rejects with assert.
template <class F>
Verdict verify_isolated(F&& checks) {
    void* shared = mmap(nullptr, sizeof(ChildClock), PROT_READ | PROT_WRITE, MAP_SHARED | MAP_ANON, -1, 0);
    if (shared == MAP_FAILED) throw std::runtime_error("mmap failed");
    ChildClock* clock = new (shared) ChildClock{};
    const bool rejected = run_isolated([&] {
        g_child_clock = clock;
        clock->start();
        checks();
        clock->stop();
    }).aborted;
    const Verdict v{rejected, clock->span};
    munmap(shared, sizeof(ChildClock));
    return v;
}

// ---------------------------------------------------------------- randomness

class Stream {
public:
    Stream(uint64_t seed, uint64_t purpose) : prng_(osuCrypto::toBlock(purpose, seed)) {}
    uint64_t next() { return prng_.get<uint64_t>(); }
    uint64_t below(uint64_t n) {
        const uint64_t limit = UINT64_MAX - UINT64_MAX % n;
        uint64_t x;
        do x = next(); while (x >= limit);
        return x % n;
    }
    double unit() { return (next() >> 11) * 0x1.0p-53; }

private:
    osuCrypto::PRNG prng_;
};

enum Purpose : uint64_t { kDatabase = 1, kIndex = 2, kFault = 3, kPublic = 4 };

// The public matrices A, and A_2 for S1 and S2. Bench mode draws them as upstream's Init and
// PreprocInit do, from system randomness. Serve and client mode draw them with upstream's
// pseudorandom, the same distribution, from seeds taken in turn from the kPublic stream, so a
// client rebuilds the server's offline state from --seed (docs/serve_protocol.md, "Session").
struct PublicSeeds {
    SeedType a, a2_q, a2_kappa;
    std::string hex;  // INFO's public_seed: the kPublic stream's key, toBlock(kPublic, seed)

    explicit PublicSeeds(uint64_t seed) {
        Stream s(seed, kPublic);
        const auto block = [&] {
            const uint64_t hi = s.next();
            return osuCrypto::toBlock(hi, s.next());
        };
        a = block();
        a2_q = block();
        a2_kappa = block();
        char buf[33];
        std::snprintf(buf, sizeof buf, "%016llx%016llx", (unsigned long long)kPublic, (unsigned long long)seed);
        hex = buf;
    }

    // Init's body (lhe.cpp:4-8) and PreprocInit's (multilimb_lhe.cpp:70-75), seeded.
    Matrix init(const VeriSimplePIR& pir) const {
        Matrix A(pir.m, LHE::n);
        pseudorandom(A, a);
        return A;
    }
    Multi_Limb_Matrix preproc_init(const VeriSimplePIR& pir) const {
        Multi_Limb_Matrix A(pir.ell, Multi_Limb_LHE::n);
        pseudorandom(A.q_data, a2_q);
        pseudorandom(A.kappa_data, a2_kappa, pir.preproc_lhe.kappa);
        return A;
    }
};

// The database: db_bytes random bytes, record r at [r * record_bytes, (r + 1) * record_bytes).
std::vector<uint8_t> database_bytes(uint64_t db_bytes, uint64_t seed) {
    std::vector<uint8_t> bytes(db_bytes);
    Stream s(seed, kDatabase);
    for (uint64_t i = 0; i < db_bytes; i += 8) {
        const uint64_t w = s.next();
        memcpy(&bytes[i], &w, std::min<uint64_t>(8, db_bytes - i));
    }
    return bytes;
}

// ---------------------------------------------------------------- layout

// The library's parameters for a database of `entries` one-byte entries, as the constructor
// computes them (preproc_pir.h:31).
PlaintextDBParams library_params(const ArmSpec& arm, uint64_t entries) {
    const Database db(entries, kEntryBits);
    return db.computeParams(kAllowTrivial, false, arm.simple_pir, 1, arm.preproc, arm.honest_hint);
}

// Entries one query's column holds: elems_per_column in PlaintextDBParams::indexToColumn.
uint64_t entries_per_column(const PlaintextDBParams& p) {
    const uint64_t logp = std::floor(std::log2(p.p));
    if (logp >= p.d) return logp / p.d * p.ell;
    return p.ell / uint64_t(std::ceil(double(p.d) / std::log2(p.p)));
}

// The one place that maps records to the library's entries (decision 18). A record is
// record_bytes consecutive one-byte entries inside one database column, so one Query and one
// Answer retrieve it, and the client decodes each entry. A column holds records_per_column
// whole records; the entries left over at the bottom of a column, and any after the last
// record, are zero padding the library still stores and computes over.
struct Layout {
    uint64_t record_bytes;        // entries per record
    uint64_t records;             // db_bytes / record_bytes
    uint64_t entries;             // N handed to the library, padding included
    uint64_t entries_per_column;  // as PlaintextDBParams::indexToColumn
    uint64_t records_per_column;

    uint64_t entry(uint64_t record, uint64_t piece) const {
        return record / records_per_column * entries_per_column + record % records_per_column * record_bytes + piece;
    }
    uint64_t columns_used() const { return (records + records_per_column - 1) / records_per_column; }
};

// The smallest library database, on a 1/1024 geometric grid from db_bytes entries up, whose
// columns fit every record whole. Upstream sizes the matrix near-square from N alone, so a
// record longer than a column forces a larger N.
std::optional<Layout> plan_layout(const ArmSpec& arm, uint64_t db_bytes, uint64_t record_bytes) {
    const uint64_t records = db_bytes / record_bytes;
    for (uint64_t n = db_bytes; n <= kMaxPadding * db_bytes; n += std::max<uint64_t>(1, n / 1024)) {
        const PlaintextDBParams p = library_params(arm, n);
        const uint64_t epc = entries_per_column(p);
        const Layout l{record_bytes, records, n, epc, epc / record_bytes};
        if (l.records_per_column == 0 || l.records_per_column * p.m < records) continue;
        if (l.entry(records - 1, record_bytes - 1) >= n) continue;
        return l;
    }
    return std::nullopt;
}

// Every record's first and last entry fall in one column by upstream's own addressing, so
// the consecutive entries between them do too.
void check_layout(const Layout& l, const PlaintextDBParams& p) {
    for (uint64_t r = 0; r < l.records; r++) {
        const uint64_t first = l.entry(r, 0), last = l.entry(r, l.record_bytes - 1);
        if (p.indexToColumn(first) != p.indexToColumn(last) || last >= l.entries) {
            std::printf("record %llu spans columns %llu..%llu\n", (unsigned long long)r,
                        (unsigned long long)p.indexToColumn(first), (unsigned long long)p.indexToColumn(last));
            abort();
        }
    }
}

// The ell x m matrix over Z_p. Each entry goes where Query and Recover look for it: row
// indexToRow(e) and column indexToColumn(e), at the bits PlaintextDBParams::recover reads
// (database.h:61-75). This is Database::packDataInMatrix's encoding, written from bytes so
// the adapter never builds upstream's per-entry BigUnsigned array (about 32 bytes an entry).
Matrix pack_database(const PlaintextDBParams& p, const Layout& l, const std::vector<uint8_t>& bytes) {
    Matrix D(p.ell, p.m);
    const uint64_t logp = std::floor(std::log2(p.p));
    for (uint64_t r = 0; r < l.records; r++) {
        for (uint64_t j = 0; j < l.record_bytes; j++) {
            const uint64_t e = l.entry(r, j), v = bytes[r * l.record_bytes + j];
            const uint64_t row = p.indexToRow(e), col = p.indexToColumn(e);
            if (logp >= p.d) {
                D.data[row * p.m + col] |= v << (p.d * (e % (logp / p.d)));
            } else {
                const uint64_t digits = std::ceil(double(p.d) / std::log2(p.p));
                for (uint64_t i = 0; i < digits; i++) D.data[(row + i) * p.m + col] = (v >> (i * logp)) & (p.p - 1);
            }
        }
    }
    return D;
}

// ---------------------------------------------------------------- faults

// One bit of a message, flipped in place. Flipping it again restores the message.
struct BitFlip {
    uint64_t elem;
    unsigned bit;
    void apply(Matrix& m) const { m.data[elem] ^= 1ULL << bit; }
};

// Where one entry lives in the decoded column: rows [first_row, first_row + rows) of the
// answer (and of the hint), and digit bits [lo, hi) of each of those Z_p elements.
// Mirrors PlaintextDBParams::indexToRow and ::recover (database.h:44-75).
struct EntryDigits {
    uint64_t first_row, rows;
    unsigned lo, hi;
};

EntryDigits entry_digits(const PlaintextDBParams& p, uint64_t entry) {
    const unsigned logp = std::log2(p.p);  // p is a power of two (database.cpp:396)
    const uint64_t row = p.indexToRow(entry);
    if (logp >= p.d) {
        const unsigned inner = entry % (logp / p.d);
        return {row, 1, unsigned(inner * p.d), unsigned(inner * p.d + p.d)};
    }
    return {row, uint64_t(std::ceil(double(p.d) / std::log2(p.p))), 0, logp};
}

// A random entry of the record.
EntryDigits record_digits(const VeriSimplePIR& pir, const Layout& l, uint64_t record, Stream& s) {
    return entry_digits(pir.dbParams, l.entry(record, s.below(l.record_bytes)));
}

// Decoding is pt = round((ans - H sk) / Delta) with Delta = q/p a power of two (lhe.h:16-21).
// Adding 2^j Delta to (ans - H sk) changes digit bit j and nothing below it, and needs no
// rounding, so flipping such a bit always changes the decoded record when j is in [lo, hi).
unsigned log_delta(const VeriSimplePIR& pir) { return __builtin_ctzll(pir.lhe.Delta); }

// flip_answer_bit: answer bit log2(Delta) + j of a record row, for a record digit bit j.
BitFlip answer_flip(const VeriSimplePIR& pir, const Layout& l, uint64_t record, Stream& s) {
    const EntryDigits r = record_digits(pir, l, record, s);
    const uint64_t row = r.first_row + s.below(r.rows);
    return {row, log_delta(pir) + unsigned(r.lo + s.below(r.hi - r.lo))};
}

// flip_hint_bit, online: H[row][col] enters decoding multiplied by sk[col] = 2^t * odd, so
// flipping hint bit b moves the digit by 2^(b + t - log2 Delta) * odd. The bit is chosen so
// that this lands on a record digit bit j, and b stays at or above log2(Delta).
BitFlip hint_flip_for_query(const VeriSimplePIR& pir, const Layout& l, const Matrix& H, const Matrix& sk,
                            uint64_t record, Stream& s) {
    for (;;) {
        const EntryDigits r = record_digits(pir, l, record, s);
        const uint64_t row = r.first_row + s.below(r.rows), col = s.below(H.cols);
        const uint64_t key = sk.data[col];
        const unsigned t = key ? __builtin_ctzll(key) : 64;
        const unsigned lo = std::max(r.lo, t);
        if (lo >= r.hi) continue;
        const unsigned j = lo + s.below(r.hi - lo);
        return {row * H.cols + col, log_delta(pir) + j - t};
    }
}

// flip_hint_bit, offline: no query exists yet, so any element of the ell x n hint, at or
// above log2(Delta).
BitFlip hint_flip_offline(const VeriSimplePIR& pir, Stream& s) {
    const unsigned ld = log_delta(pir);
    return {s.below(pir.ell * LHE::n), ld + unsigned(s.below(64 - ld))};
}

// bad_proof: any element of the 40 x ell proof (PreprocProve's output), at a bit below its
// norm bound p*m, so PreprocVerify sees a changed value.
BitFlip proof_flip(const VeriSimplePIR& pir, Stream& s) {
    return {s.below(VeriSimplePIR::stat_sec_param * pir.ell), unsigned(s.below(wire_bits(pir).proof))};
}

// An offline fault: one bit of the hint (flip_hint_bit) or of the proof (bad_proof).
struct OfflineFlip {
    FaultKind kind;
    BitFlip bit;
};

OfflineFlip offline_flip(const VeriSimplePIR& pir, FaultKind kind, Stream& s) {
    return {kind, kind == kBadProof ? proof_flip(pir, s) : hint_flip_offline(pir, s)};
}

// ---------------------------------------------------------------- the scheme

std::unique_ptr<VeriSimplePIR> make_pir(const ArmSpec& arm, const Layout& l) {
    return std::make_unique<VeriSimplePIR>(l.entries, kEntryBits, kAllowTrivial, false, arm.simple_pir,
                                           /*randomData=*/false, /*batchSize=*/1, arm.preproc, arm.honest_hint);
}

// Layout and parameter selection, in a child because upstream rejects parameters with
// assert: the layout search, the constructor (computeParams, checkSISHardness), and the
// packed answer kernel's width limit (mat_packed.cpp:39-43).
std::pair<std::optional<Layout>, std::string> select_params(const ArmSpec& arm, uint64_t db_bytes,
                                                            uint64_t record_bytes) {
    return run_isolated_value<Layout>([&] {
        const std::optional<Layout> l = plan_layout(arm, db_bytes, record_bytes);
        if (!l) {
            std::printf("no library database up to %llu entries per byte holds every %llu-byte record in one column\n",
                        (unsigned long long)kMaxPadding, (unsigned long long)record_bytes);
            abort();
        }
        const auto pir = make_pir(arm, *l);
        check_layout(*l, pir->dbParams);
        packMatrixHardCoded(1, 1, pir->dbParams.p, false);
        return *l;
    });
}

// The database in the library's matrix forms. Built once and never timed, like the
// database itself: D (ell x m), D^T for the preprocessing, and D packed for Answer. For
// stale_record also the stale server's packed database: the same layout over the bytes with
// every record bit flipped.
struct ServerDB {
    Matrix D, D_T;
    std::unique_ptr<PackedMatrix> D_packed, stale_packed;

    ServerDB(const VeriSimplePIR& pir, const ArmSpec& arm, const Layout& l, const std::vector<uint8_t>& bytes,
             bool with_stale) {
        D = pack_database(pir.dbParams, l, bytes);
        if (arm.preproc) D_T = transpose(D);
        D_packed.reset(new PackedMatrix(packMatrixHardCoded(D, pir.dbParams.p)));
        if (with_stale) {
            std::vector<uint8_t> stale(bytes);
            for (uint8_t& b : stale) b = ~b;
            Matrix S = pack_database(pir.dbParams, l, stale);
            stale_packed.reset(new PackedMatrix(packMatrixHardCoded(S, pir.dbParams.p)));
            release(S);
        }
    }
    ServerDB(const ServerDB&) = delete;
};

// What the online phase needs from setup: the client's state and nothing else.
struct ClientState {
    Matrix A, H;                     // public matrix, hint
    Matrix Z;                        // S1/S2: preprocessed check matrix, 40 x m
    std::unique_ptr<BinaryMatrix> C;  // S1/S2: secret binary matrix, 40 x ell
    uint64_t storage_bytes() const {
        return memory_bytes(A) + memory_bytes(H) + (C ? memory_bytes(Z) + memory_bytes(*C) : 0);
    }
    ClientState() { A.data = H.data = Z.data = nullptr; }
    ClientState(const ClientState&) = delete;
    ~ClientState() {
        free(A.data);
        free(H.data);
        free(Z.data);
    }
};

// One offline repetition's record fields. Trivially copyable, because a timed repetition
// runs in a forked child and returns it through shared memory.
struct OfflineMetrics {
    Span public_params, server_preproc;
    std::optional<Span> server_proof;  // S1/S2
    Span client;                       // 0 for S0-cpp, which has no client offline work
    uint64_t down_bytes = 0, hint_bytes = 0, up_bytes = 0, storage_bytes = 0;
    std::optional<bool> detected;      // verifiable arms
    uint64_t peak_rss_bytes = 0;

    Span total() const {
        Span s = public_params;
        s += server_preproc;
        if (server_proof) s += *server_proof;
        s += client;
        return s;
    }
};

struct OfflineResult {
    OfflineMetrics metrics;
    std::unique_ptr<ClientState> state;
};

OfflineResult run_offline(const VeriSimplePIR& pir, const ArmSpec& arm, const ServerDB& db,
                          std::optional<OfflineFlip> flip, const PublicSeeds* seeds = nullptr) {
    OfflineResult out;
    OfflineMetrics& r = out.metrics;
    out.state = std::make_unique<ClientState>();
    ClientState& c = *out.state;
    const WireBits w = wire_bits(pir);

    timed(r.public_params, [&] { c.A = seeds ? seeds->init(pir) : pir.Init(); });
    timed(r.server_preproc, [&] { c.H = pir.GenerateHint(c.A, db.D); });

    if (!arm.preproc) {
        // SimplePIR has no client offline work, and no check a bad hint could fail.
        r.down_bytes = wire_bytes(c.A, w.q) + wire_bytes(c.H, w.q);
        r.hint_bytes = wire_bytes(c.H, w.q);
        r.storage_bytes = c.storage_bytes();
        return out;
    }

    Multi_Limb_Matrix A2(0, 0), H2(0, 0);
    unsigned char server_hash[SHA256_DIGEST_LENGTH], client_hash[SHA256_DIGEST_LENGTH];
    timed(r.public_params, [&] { A2 = seeds ? seeds->preproc_init(pir) : pir.PreprocInit(); });
    timed(r.server_preproc, [&] {
        H2 = pir.PreprocGenerateHint(A2, db.D_T);
        pir.HashAandH(server_hash, A2, H2);
    });

    Span client;
    std::pair<std::vector<Multi_Limb_Matrix>, std::vector<Multi_Limb_Matrix>> cts_sks;
    timed(client, [&] {
        pir.HashAandH(client_hash, A2, H2);
        c.C.reset(new BinaryMatrix(pir.PreprocSampleC()));
        cts_sks = pir.PreprocClientMessage(A2, *c.C);
    });
    auto& cts = cts_sks.first;
    auto& sks = cts_sks.second;

    Span proof_span;
    std::vector<Multi_Limb_Matrix> res;
    Matrix proof;
    timed(proof_span, [&] {
        res = pir.PreprocAnswer(cts, db.D_T);
        proof = pir.PreprocProve(server_hash, cts, res, db.D_T);
    });
    r.server_proof = proof_span;

    if (!flip) {
        timed(client, [&] {
            pir.PreprocVerify(A2, H2, client_hash, cts, res, proof);
            c.Z = pir.PreprocRecoverZ(H2, sks, res);
            pir.VerifyPreprocZ(c.Z, c.A, *c.C, c.H);
        });
        r.detected = false;
    } else {
        Matrix& message = flip->kind == kBadProof ? proof : c.H;
        flip->bit.apply(message);
        const Verdict v = verify_isolated([&] {
            pir.PreprocVerify(A2, H2, client_hash, cts, res, proof);
            Matrix Z = pir.PreprocRecoverZ(H2, sks, res);
            pir.VerifyPreprocZ(Z, c.A, *c.C, c.H);
        });
        flip->bit.apply(message);
        client += v.span;
        r.detected = v.rejected;
    }
    r.client = client;

    r.down_bytes = wire_bytes(c.A, w.q) + wire_bytes(c.H, w.q) + wire_bytes(A2, w.multi_limb) +
                   wire_bytes(H2, w.multi_limb) + wire_bytes(res, w.multi_limb) + wire_bytes(proof, w.proof);
    // params.cpp's "hint download"
    r.hint_bytes = wire_bytes(c.H, w.q) + wire_bytes(H2, w.multi_limb) + wire_bytes(proof, w.proof);
    r.up_bytes = wire_bytes(cts, w.multi_limb);
    r.storage_bytes = c.storage_bytes();

    release(A2);
    release(H2);
    release(cts);
    release(sks);
    release(res);
    release(proof);
    return out;
}

// A timed offline repetition in this process, with this process's peak RSS. Each S1/S2
// offline phase leaks tens of MB inside the library (multilimb_lhe.cpp:14-17; Matrix has no
// destructor), so repetitions here accumulate it.
OfflineMetrics run_offline_here(const VeriSimplePIR& pir, const ArmSpec& arm, const ServerDB& db,
                                std::optional<OfflineFlip> flip) {
    OfflineMetrics r = run_offline(pir, arm, db, flip).metrics;
    r.peak_rss_bytes = peak_rss_bytes();
    return r;
}

// The same repetition in a forked child, so the leak dies with it. The peak RSS is the child's.
OfflineMetrics run_offline_isolated(const VeriSimplePIR& pir, const ArmSpec& arm, const ServerDB& db,
                                    std::optional<OfflineFlip> flip) {
    auto [m, output] = run_isolated_value<OfflineMetrics>([&] { return run_offline_here(pir, arm, db, flip); });
    if (!m) throw std::runtime_error("offline repetition aborted: " + output);
    return *m;
}

// Recover's body (preproc_pir.cpp:392-404) for every entry of the record: one decryption of
// the column, then PlaintextDBParams::recover per entry. Recover itself would decrypt the
// whole column again for each entry.
std::vector<uint64_t> decode_record(const VeriSimplePIR& pir, const Layout& l, const Matrix& H, const Matrix& ans,
                                    const Matrix& sk, uint64_t record) {
    Matrix pt = pir.lhe.decrypt(H, sk, ans);
    std::vector<uint64_t> out(l.record_bytes);
    for (uint64_t j = 0; j < l.record_bytes; j++) {
        const uint64_t e = l.entry(record, j);
        out[j] = pir.dbParams.recover(&pt.data[pir.dbParams.indexToRow(e)], e).toUnsignedLong();
    }
    release(pt);
    return out;
}

std::vector<uint64_t> truth_of(const Layout& l, const std::vector<uint8_t>& bytes, uint64_t record) {
    const auto first = bytes.begin() + record * l.record_bytes;
    return std::vector<uint64_t>(first, first + l.record_bytes);
}

// One query and its answer. Trivially copyable, because a re_preprocess attempt runs it in a
// forked child and returns it through shared memory.
struct QueryResult {
    Span query_gen, answer;  // client mode: answer.wall is the server's answer_ns, and its CPU time is unknown
    std::optional<Span> verify, recover;
    uint64_t up_bytes = 0, down_bytes = 0;
    std::optional<bool> correct, detected;
    Span rtt;  // client mode: from the first byte of QUERY sent to the last byte of ANSWER received

    bool accepted() const { return correct.has_value(); }  // the client decodes only what it accepts
    Span total() const {
        Span s = query_gen;
        s += answer;
        for (const auto& part : {verify, recover})
            if (part) s += *part;
        return s;
    }
};

struct QueryChecks {
    bool fault_effect;      // the fault changes what an honest, non-verifying client decodes
    bool upstream_recover;  // upstream's own Recover agrees with decode_record
};

QueryResult run_query(const VeriSimplePIR& pir, const ArmSpec& arm, const Layout& l, const ServerDB& db,
                      ClientState& c, const std::vector<uint8_t>& bytes, uint64_t record, Stream& faults,
                      std::optional<FaultKind> fault, QueryChecks checks) {
    QueryResult r;
    const WireBits w = wire_bits(pir);
    std::pair<Matrix, Matrix> ct_sk;
    timed(r.query_gen, [&] { ct_sk = pir.Query(c.A, l.entry(record, 0)); });
    Matrix& ct = ct_sk.first;
    Matrix& sk = ct_sk.second;
    r.up_bytes = wire_bytes(ct, w.q);

    Matrix ans;
    timed(r.answer, [&] { ans = pir.Answer(ct, *db.D_packed); });
    if (fault == kDropServer) {
        // The server withholds its answer, and the client counts the missing answer as a rejection.
        if (arm.verifiable()) r.detected = true;
        release(ans);
        release(ct);
        release(sk);
        return r;
    }
    r.down_bytes = wire_bytes(ans, w.q);

    const std::vector<uint64_t> truth = truth_of(l, bytes, record);
    if (checks.upstream_recover) {
        const std::vector<uint64_t> got = decode_record(pir, l, c.H, ans, sk, record);
        for (uint64_t j : {uint64_t(0), l.record_bytes - 1})
            if (pir.Recover(c.H, ans, sk, l.entry(record, j)).toUnsignedLong() != got[j] || got[j] != truth[j])
                throw std::runtime_error("upstream Recover disagrees with the decoded record at piece " +
                                         std::to_string(j));
    }

    std::optional<BitFlip> hint_flip;
    if (fault == kFlipAnswerBit) {
        answer_flip(pir, l, record, faults).apply(ans);
    } else if (fault == kWrongRecord) {
        uint64_t other = faults.below(l.records - 1);
        if (other >= record) other++;
        auto [ct2, sk2] = pir.Query(c.A, l.entry(other, 0));
        release(ans);
        ans = pir.Answer(ct2, *db.D_packed);
        release(ct2);
        release(sk2);
    } else if (fault == kStaleRecord) {
        release(ans);
        ans = pir.Answer(ct, *db.stale_packed);
    } else if (fault == kFlipHintBit) {
        hint_flip = hint_flip_for_query(pir, l, c.H, sk, record, faults);
        hint_flip->apply(c.H);
    }
    if (checks.fault_effect && decode_record(pir, l, c.H, ans, sk, record) == truth)
        throw std::runtime_error(std::string("--fault ") + (fault == kFlipHintBit ? "flip_hint_bit" : "flip_answer_bit") +
                                 " left the honest client's output unchanged on the first faulted repetition");

    if (arm.verifiable()) {
        if (fault) {
            // A client checks every hint it accepts against Z (VerifyPreprocZ) before using it.
            const Verdict v = verify_isolated([&] {
                if (hint_flip) pir.VerifyPreprocZ(c.Z, c.A, *c.C, c.H);
                pir.PreVerify(ct, ans, c.Z, *c.C);
            });
            r.verify = v.span;
            r.detected = v.rejected;
        } else {
            timed(r.verify.emplace(), [&] { pir.PreVerify(ct, ans, c.Z, *c.C); });
            r.detected = false;
        }
    }

    if (!r.detected.value_or(false)) {
        std::vector<uint64_t> got;
        timed(r.recover.emplace(), [&] { got = decode_record(pir, l, c.H, ans, sk, record); });
        r.correct = got == truth;
    }

    if (hint_flip) hint_flip->apply(c.H);
    release(ct);
    release(sk);
    release(ans);
    return r;
}

std::optional<FaultKind> fault_if(const Options& o, bool faulted) {
    return faulted ? std::optional(o.fault->kind) : std::nullopt;
}

enum class Outcome { Accepted, Recovered, Aborted, SilentWrong };
constexpr const char* kOutcomeNames[] = {"accepted", "recovered", "aborted", "silent_wrong"};  // Outcome's order

// What the client did after its first answer (src/adapters/README.md, "Recovery").
struct Recovery {
    Outcome outcome = Outcome::Accepted;
    uint64_t attempts = 0;
    Span span;  // every further attempt's scheme calls, the offline phase included for re_preprocess
    uint64_t up_bytes = 0, down_bytes = 0;
};

// A re_preprocess attempt: a whole offline phase, then a query. Trivially copyable, because it
// runs in a forked child and returns through shared memory.
struct Reprocessed {
    OfflineMetrics offline;
    QueryResult query;
};

// After a rejected first answer, the policy's further attempts. Each attempt's server is
// faulted with probability --fault-prob, except an honest replica, which never is. A
// re_preprocess attempt runs in a child, because each offline phase leaks tens of MiB in the
// library and its child's state is thrown away anyway. Its fault choices come from a stream
// seeded from the fault stream, so the parent's stream advances the same on every run.
Recovery run_recovery(const Options& o, const VeriSimplePIR& pir, const Layout& l, const ServerDB& db,
                      ClientState& c, const std::vector<uint8_t>& bytes, uint64_t record, Stream& faults,
                      const QueryResult& first) {
    Recovery rec;
    if (first.accepted()) {
        rec.outcome = *first.correct ? Outcome::Accepted : Outcome::SilentWrong;
        return rec;
    }
    rec.outcome = Outcome::Aborted;
    const ArmSpec& arm = *o.arm;
    while (o.recovery->kind != Policy::Abort && rec.attempts < kRecoveryBudget) {
        rec.attempts++;
        QueryResult q;
        if (o.recovery->kind == Policy::SwitchReplica) {
            const bool faulted = o.replicas_cheat && faults.unit() < o.fault_prob;
            q = run_query(pir, arm, l, db, c, bytes, record, faults, fault_if(o, faulted), QueryChecks{});
        } else {
            const bool faulted = faults.unit() < o.fault_prob;
            const uint64_t attempt_seed = faults.next();
            auto [attempt, output] = run_isolated_value<Reprocessed>([&] {
                Stream attempt_faults(attempt_seed, kFault);
                OfflineResult fresh = run_offline(pir, arm, db, std::nullopt);
                return Reprocessed{fresh.metrics, run_query(pir, arm, l, db, *fresh.state, bytes, record, attempt_faults,
                                                            fault_if(o, faulted), QueryChecks{})};
            });
            if (!attempt) throw std::runtime_error("re_preprocess attempt aborted: " + output);
            rec.span += attempt->offline.total();
            rec.up_bytes += attempt->offline.up_bytes;
            rec.down_bytes += attempt->offline.down_bytes;
            q = attempt->query;
        }
        rec.span += q.total();
        rec.up_bytes += q.up_bytes;
        rec.down_bytes += q.down_bytes;
        if (q.accepted()) {
            rec.outcome = *q.correct ? Outcome::Recovered : Outcome::SilentWrong;
            break;
        }
    }
    return rec;
}

// ---------------------------------------------------------------- records

Json num(double v) { return Json::number(v); }

void put_span(Json::Fields& f, const std::string& name, const std::optional<Span>& s) {
    f.emplace_back(name + "_s", s ? num(s->wall) : Json::null());
    f.emplace_back(name + "_cpu_s", s ? num(s->cpu) : Json::null());
}

// Every record carries every metric key; the other phase's are null, and so are the recovery
// metrics unless a further attempt was made. An offline record's peak RSS is its child's; an
// online record's is this process's. A client-mode record adds rtt_s and rtt_cpu_s, and its
// server_answer_cpu_s entries are null because the ANSWER frame carries only answer_ns.
Json metrics(Phase phase, const OfflineMetrics* off, const QueryResult* on, const Recovery* rec,
             Mode mode = Mode::Bench) {
    const bool client = mode == Mode::Client;
    const auto span = [](const OfflineMetrics* r, Span OfflineMetrics::*m) {
        return r ? std::optional<Span>(r->*m) : std::nullopt;
    };
    const auto integer = [](bool have, uint64_t v) { return have ? Json::integer(v) : Json::null(); };
    const auto one = [](bool have, Json v) { return have ? Json::list({v}) : Json::null(); };
    Json::Fields f;
    put_span(f, "public_params", span(off, &OfflineMetrics::public_params));
    put_span(f, "server_preproc", span(off, &OfflineMetrics::server_preproc));
    put_span(f, "server_preproc_proof", off ? off->server_proof : std::nullopt);
    put_span(f, "client_offline", span(off, &OfflineMetrics::client));
    f.emplace_back("offline_down_bytes", integer(off, off ? off->down_bytes : 0));
    f.emplace_back("offline_down_bytes_hint_only", integer(off, off ? off->hint_bytes : 0));
    f.emplace_back("offline_up_bytes", integer(off, off ? off->up_bytes : 0));
    f.emplace_back("client_storage_bytes", integer(off, off ? off->storage_bytes : 0));
    put_span(f, "query_gen", on ? std::optional<Span>(on->query_gen) : std::nullopt);
    f.emplace_back("up_bytes", integer(on, on ? on->up_bytes : 0));
    f.emplace_back("up_bytes_per_server", one(on, Json::integer(on ? on->up_bytes : 0)));
    f.emplace_back("server_answer_s", one(on, num(on ? on->answer.wall : 0)));
    f.emplace_back("server_answer_cpu_s", one(on, client ? Json::null() : num(on ? on->answer.cpu : 0)));
    f.emplace_back("down_bytes", integer(on, on ? on->down_bytes : 0));
    f.emplace_back("down_bytes_per_server", one(on, Json::integer(on ? on->down_bytes : 0)));
    put_span(f, "client_recover", on ? on->recover : std::nullopt);
    put_span(f, "client_verify", on ? on->verify : std::nullopt);
    f.emplace_back("verify_fused", phase == Phase::Online ? Json::boolean(false) : Json::null());
    const bool retried = rec && rec->attempts > 0;
    put_span(f, "recovery", retried ? std::optional<Span>(rec->span) : std::nullopt);
    f.emplace_back("recovery_up_bytes", integer(retried, retried ? rec->up_bytes : 0));
    f.emplace_back("recovery_down_bytes", integer(retried, retried ? rec->down_bytes : 0));
    f.emplace_back("process_peak_rss_bytes", Json::integer(off ? off->peak_rss_bytes : peak_rss_bytes()));
    if (client) {
        f.emplace_back("rtt_s", one(on, num(on ? on->rtt.wall : 0)));
        f.emplace_back("rtt_cpu_s", one(on, num(on ? on->rtt.cpu : 0)));
    }
    return Json::object(f);
}

// kappa as the constructor computes it (preproc_pir.h:43-44); upstream's own variable.
Elem constructor_kappa(const VeriSimplePIR& pir) { return pir.preproc_lhe.kappa; }

// kappa as params.cpp:50 computes it for its size formulas.
uint64_t params_cpp_kappa(const PlaintextDBParams& p) {
    return p.ell * (Elem)std::ceil(std::sqrt(((double)p.ell) / ((double)p.m)));
}

Json scheme_params(const ArmSpec& arm, const Options& o, const std::optional<Layout>& l, const VeriSimplePIR* pir) {
    const auto maybe = [](bool have, Json v) { return have ? v : Json::null(); };
    Json::Fields f = {
        {"entry_bits", Json::integer(kEntryBits)},
        {"pieces_per_record", Json::integer(o.record_bytes)},
        {"N", maybe(bool(l), Json::integer(l ? l->entries : 0))},
        {"entries_per_column", maybe(bool(l), Json::integer(l ? l->entries_per_column : 0))},
        {"records_per_column", maybe(bool(l), Json::integer(l ? l->records_per_column : 0))},
        {"columns_used", maybe(bool(l), Json::integer(l ? l->columns_used() : 0))},
        {"padding_factor", maybe(bool(l), num(l ? double(l->entries) / double(o.db_bytes) : 0))},
        {"decode", Json::string("one decryption, recover per entry")},
        {"n", Json::integer(LHE::n)},
        {"logq", Json::integer(LHE::logq)},
        {"simple_pir", Json::boolean(arm.simple_pir)},
        {"preproc", Json::boolean(arm.preproc)},
        {"honest_hint", arm.verifiable() ? Json::boolean(arm.honest_hint) : Json::null()},
        {"allow_trivial", Json::boolean(kAllowTrivial)},
        {"stat_sec_param", arm.verifiable() ? Json::integer(STAT_SEC_PARAM) : Json::null()},
        {"answer_kernel", Json::string(kAnswerKernel)},
        {"offline_in_process", Json::boolean(o.offline_in_process)},
    };
    if (pir) {
        const PlaintextDBParams& p = pir->dbParams;
        const WireBits w = wire_bits(*pir);
        f.emplace_back("p", Json::integer(p.p));
        f.emplace_back("log2_p", num(std::log2(double(p.p))));
        f.emplace_back("m", Json::integer(p.m));
        f.emplace_back("ell", Json::integer(p.ell));
        f.emplace_back("kappa", arm.verifiable() ? Json::integer(constructor_kappa(*pir)) : Json::null());
        f.emplace_back("kappa_params_cpp", arm.verifiable() ? Json::integer(params_cpp_kappa(p)) : Json::null());
        f.emplace_back("wire_bits_q", Json::integer(w.q));
        f.emplace_back("wire_bits_multi_limb", arm.verifiable() ? Json::integer(w.multi_limb) : Json::null());
        f.emplace_back("wire_bits_proof", arm.verifiable() ? Json::integer(w.proof) : Json::null());
    } else {
        for (const char* k : {"p", "log2_p", "m", "ell", "kappa", "kappa_params_cpp", "wire_bits_q",
                              "wire_bits_multi_limb", "wire_bits_proof"})
            f.emplace_back(k, Json::null());
    }
    // Spec 9: the server's W workers answer different queries; the library has no intra-query threads.
    if (o.mode != Mode::Bench) f.emplace_back("parallelism", Json::string("inter_query"));
    return Json::object(f);
}

// Upstream's size formulas, src/demo/scripts/params.cpp:42-74, with the same integer
// truncation and params.cpp's own kappa (:50). Bits there, bytes here. Every record carries
// every key. offline_down_bytes is null: params.cpp counts no public matrix.
Json analytical(const ArmSpec& arm, const PlaintextDBParams& p) {
    const uint64_t logq = LHE::logq, n = LHE::n;
    const uint64_t hintSize_bits = logq * n * p.ell;
    Json::Fields f = {
        {"up_bytes", num(double(logq * p.m) / 8)},
        {"down_bytes", num(double(logq * p.ell) / 8)},
        {"offline_down_bytes", Json::null()},
        {"client_storage_bytes", num(double(hintSize_bits) / 8)},
    };
    if (!arm.preproc) {
        f.emplace_back("offline_down_bytes_hint_only", num(double(hintSize_bits) / 8));
        f.emplace_back("offline_up_bytes", Json::null());
        f.emplace_back("offline_preproc_down_bytes", Json::null());
        return Json::object(f);
    }
    const uint64_t kappa = params_cpp_kappa(p);
    const double log_kappa = log2(kappa);
    const uint64_t additional_hint_size = (logq + log_kappa) * p.m * n;
    const uint64_t Z_t_size = log2(p.p * p.m) * p.ell * STAT_SEC_PARAM;
    const uint64_t ciphertext_upload_size = (logq + log_kappa) * p.ell * STAT_SEC_PARAM;
    const uint64_t ciphertext_download_size = (logq + log_kappa) * p.m * STAT_SEC_PARAM;
    const uint64_t preproc_proof_size = log2(p.p * p.m) * p.ell * STAT_SEC_PARAM;
    f.emplace_back("offline_down_bytes_hint_only",
                   num(double(hintSize_bits + additional_hint_size + Z_t_size) / 8));
    f.emplace_back("offline_up_bytes", num(double(ciphertext_upload_size) / 8));
    f.emplace_back("offline_preproc_down_bytes", num(double(ciphertext_download_size + preproc_proof_size) / 8));
    return Json::object(f);
}

std::optional<std::string> env(const char* name) {
    const char* v = std::getenv(name);
    if (!v) return std::nullopt;
    return std::string(v);
}

const char* mode_name(Mode m) {
    for (const auto& s : kModes)
        if (s.mode == m) return s.name;
    return "?";
}

// One record. Run-wide fields are fixed at construction; each repetition fills the rest.
// Bench records carry no mode field; client records add "mode": "client".
struct Record {
    const Options& o;
    Json scheme_params;
    Phase phase = Phase::Offline;
    uint64_t rep = 0;
    bool faulted = false;
    std::string status = "ok";
    Json metrics = Json::null(), analytical = Json::null();
    std::optional<bool> correct, detected;
    std::optional<Recovery> recovery;  // online records of a supported run
    std::optional<std::string> error;

    std::string to_json() const {
        const auto str = [](const std::string& s) { return Json::string(s); };
        const auto boolean = [](bool b) { return Json::boolean(b); };
        Json::Fields f = {
            {"run_id", Json::optional(env("VPIR_RUN_ID"), str)},
            {"git_sha_harness", Json::optional(env("VPIR_GIT_SHA"), str)},
            {"platform", Json::optional(env("VPIR_PLATFORM"), str)},
            {"upstream_sha", Json::string(UPSTREAM_SHA)},
            {"adapter", Json::string("vsp_cpp")},
            {"arm", Json::string(o.arm->name)},
            {"scheme_params", scheme_params},
            {"db_bytes", Json::integer(o.db_bytes)},
            {"record_bytes", Json::integer(o.record_bytes)},
            {"retrieved_bits", Json::integer(8 * o.record_bytes)},
            {"n_records", Json::integer(o.db_bytes / o.record_bytes)},
            {"phase", Json::string(phase == Phase::Offline ? "offline" : "online")},
            {"rep", Json::integer(rep)},
            {"is_warmup", Json::boolean(rep < o.warmup)},
            {"seed", Json::integer(o.seed)},
            {"fault", Json::string(o.fault->name)},
            {"fault_prob", num(o.fault_prob)},
            {"faulted", Json::boolean(faulted)},
            {"recovery", Json::string(o.recovery->name)},
            {"recovery_outcome",
             recovery ? Json::string(kOutcomeNames[static_cast<int>(recovery->outcome)]) : Json::null()},
            {"recovery_attempts", recovery ? Json::integer(recovery->attempts) : Json::null()},
            {"status", Json::string(status)},
            {"metrics", metrics},
            {"analytical", analytical},
            {"correct", Json::optional(correct, boolean)},
            {"detected", Json::optional(detected, boolean)},
            {"error", Json::optional(error, str)},
        };
        if (o.mode != Mode::Bench) f.emplace_back("mode", Json::string(mode_name(o.mode)));
        return Json::object(f).text();
    }
};

class Sink {
public:
    explicit Sink(const std::string& path) : f_(std::fopen(path.c_str(), "a")) {
        if (!f_) throw std::runtime_error("cannot open --out " + path + ": " + std::strerror(errno));
    }
    ~Sink() { std::fclose(f_); }
    void write(const Record& r) { write(r.to_json()); }
    void write(const std::string& record) {
        const std::string line = record + "\n";
        if (std::fwrite(line.data(), 1, line.size(), f_) != line.size() || std::fflush(f_))
            throw std::runtime_error("write to --out failed");
    }

private:
    FILE* f_;
};

// ---------------------------------------------------------------- driver

bool draw_faulted(const Options& o, Phase phase, Stream& faults) {
    if (!o.fault->in(phase)) return false;
    return faults.unit() < o.fault_prob;
}

void write_unsupported(const Options& o, const std::string& why, Sink& sink) {
    for (Phase phase : {Phase::Offline, Phase::Online}) {
        if ((phase == Phase::Offline && !o.offline) || (phase == Phase::Online && !o.online)) continue;
        for (uint64_t rep = 0; rep < o.warmup + o.reps; rep++) {
            Record r{o, scheme_params(*o.arm, o, std::nullopt, nullptr)};  // analytical stays null
            r.phase = phase;
            r.rep = rep;
            r.status = "unsupported_params";
            r.metrics = metrics(phase, nullptr, nullptr, nullptr, o.mode);
            r.error = why;
            sink.write(r);
        }
    }
}

int run_bench(const Options& o, const Layout& l, Sink& sink) {
    const ArmSpec& arm = *o.arm;
    const uint64_t total = o.warmup + o.reps;
    const auto pir = make_pir(arm, l);
    const std::vector<uint8_t> bytes = database_bytes(o.db_bytes, o.seed);
    const ServerDB db(*pir, arm, l, bytes, o.fault->kind == kStaleRecord);
    const Json params = scheme_params(arm, o, l, pir.get()), sizes = analytical(arm, pir->dbParams);
    Stream index_stream(o.seed, kIndex), fault_stream(o.seed, kFault);

    if (o.offline) {
        for (uint64_t rep = 0; rep < total; rep++) {
            Record r{o, params};
            r.phase = Phase::Offline;
            r.rep = rep;
            r.faulted = draw_faulted(o, Phase::Offline, fault_stream);
            const std::optional<OfflineFlip> flip =
                r.faulted ? std::optional(offline_flip(*pir, o.fault->kind, fault_stream)) : std::nullopt;
            const OfflineMetrics m = o.offline_in_process ? run_offline_here(*pir, arm, db, flip)
                                                          : run_offline_isolated(*pir, arm, db, flip);
            r.metrics = metrics(Phase::Offline, &m, nullptr, nullptr);
            r.analytical = sizes;
            r.detected = m.detected;
            sink.write(r);
        }
        std::fprintf(stderr, "%s: %llu offline reps written\n", arm.name, (unsigned long long)total);
    }

    if (o.online) {
        const std::unique_ptr<ClientState> state = run_offline(*pir, arm, db, std::nullopt).state;
        bool effect_checked = false;
        for (uint64_t rep = 0; rep < total; rep++) {
            Record r{o, params};
            r.phase = Phase::Online;
            r.rep = rep;
            const uint64_t record = index_stream.below(l.records);
            r.faulted = draw_faulted(o, Phase::Online, fault_stream);
            const bool check = r.faulted && (o.fault->kind & (kFlipAnswerBit | kFlipHintBit)) && !effect_checked;
            effect_checked |= check;
            const QueryResult q = run_query(*pir, arm, l, db, *state, bytes, record, fault_stream,
                                            fault_if(o, r.faulted), {check, /*upstream_recover=*/rep == 0});
            r.recovery = run_recovery(o, *pir, l, db, *state, bytes, record, fault_stream, q);
            r.metrics = metrics(Phase::Online, nullptr, &q, &*r.recovery);
            r.analytical = sizes;
            r.correct = q.correct;
            r.detected = q.detected;
            sink.write(r);
        }
        std::fprintf(stderr, "%s: %llu online reps written\n", arm.name, (unsigned long long)total);
    }
    return 0;
}

// ---------------------------------------------------------------- serve and client mode
//
// docs/serve_protocol.md. Queries and answers travel in the bench-mode encodings: a query is
// ct's m elements and an answer is ans's ell elements, each a 64-bit word, big-endian.

uint64_t now_ns() {
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return uint64_t(ts.tv_sec) * 1000000000ULL + ts.tv_nsec;
}

std::string sha256(const void* data, size_t n) {
    unsigned char md[EVP_MAX_MD_SIZE];
    unsigned len = 0;
    if (!EVP_Digest(data, n, md, &len, EVP_sha256(), nullptr)) throw std::runtime_error("SHA-256 failed");
    return std::string(reinterpret_cast<const char*>(md), len);
}

std::string hex(const std::string& bytes) {
    static const char digits[] = "0123456789abcdef";
    std::string out;
    for (unsigned char b : bytes) {
        out += digits[b >> 4];
        out += digits[b & 15];
    }
    return out;
}

void put_elems(std::string& out, const Matrix& m) {
    const size_t at = out.size(), n = m.rows * m.cols;
    out.resize(at + 8 * n);
    auto* p = reinterpret_cast<uint8_t*>(&out[at]);
    for (size_t i = 0; i < n; i++)
        for (int b = 0; b < 8; b++) p[8 * i + b] = uint8_t(m.data[i] >> (56 - 8 * b));
}

void get_elems(const uint8_t* p, Matrix& m) {
    for (size_t i = 0; i < m.rows * m.cols; i++) m.data[i] = wire::get_be(p + 8 * i, 8);
}

// INFO's fields but scheme_params (docs/serve_protocol.md, "Session"). The client computes
// them too and compares kFingerprint with the server's.
Json::Fields info_fields(const Options& o, const std::vector<uint8_t>& bytes, const PublicSeeds& seeds) {
    return {
        {"protocol", Json::integer(1)},
        {"arm", Json::string(o.arm->name)},
        {"server_id", Json::integer(0)},
        {"servers", Json::integer(1)},
        {"db_bytes", Json::integer(o.db_bytes)},
        {"record_bytes", Json::integer(o.record_bytes)},
        {"n_records", Json::integer(o.db_bytes / o.record_bytes)},
        {"seed", Json::integer(o.seed)},
        {"workers", Json::integer(o.workers)},
        {"upstream_sha", Json::string(UPSTREAM_SHA)},
        {"db_sha256", Json::string(hex(sha256(bytes.data(), bytes.size())))},
        {"public_seed", Json::string(seeds.hex)},
    };
}

constexpr const char* kFingerprint[] = {"arm", "db_bytes", "record_bytes", "seed", "db_sha256", "public_seed"};

// ------------------------------------------------ the server

// The server's half of the offline phase: the calls bench mode times as public_params and
// server_preproc. It runs once at startup and stays resident, as a server keeps the hint it
// hands to new clients. The protocol sends none of it, because each client rebuilds it.
struct ServerHint {
    Matrix A, H;
    Multi_Limb_Matrix A2{0, 0}, H2{0, 0};  // S1, S2
    unsigned char hash[SHA256_DIGEST_LENGTH] = {};
};

std::unique_ptr<ServerHint> server_offline(const VeriSimplePIR& pir, const ArmSpec& arm, const ServerDB& db,
                                           const PublicSeeds& seeds) {
    auto h = std::make_unique<ServerHint>();
    h->A = seeds.init(pir);
    h->H = pir.GenerateHint(h->A, db.D);
    if (arm.preproc) {
        h->A2 = seeds.preproc_init(pir);
        h->H2 = pir.PreprocGenerateHint(h->A2, db.D_T);
        pir.HashAandH(h->hash, h->A2, h->H2);
    }
    return h;
}

// One client connection. Its reader thread and the workers answering its queries all write
// frames to it, so a write holds the lock. The socket closes when the last of them lets go.
class Connection {
public:
    explicit Connection(int fd) : fd(fd) {}
    ~Connection() { close(fd); }
    Connection(const Connection&) = delete;

    // Whether the whole frame was written.
    bool send(const std::string& frame) {
        std::lock_guard<std::mutex> lock(mu_);
        if (!broken_ && !wire::write_all(fd, frame)) broken_ = true;
        return !broken_;
    }

    const int fd;
    std::atomic<uint64_t> pending{0};  // its queries read and not yet answered

private:
    std::mutex mu_;
    bool broken_ = false;  // a write failed, so the client has gone and later frames are dropped
};

// One QUERY on its way from a reader to a worker.
struct Job {
    std::shared_ptr<Connection> conn;
    uint64_t req_id;
    std::vector<Elem> ct;  // the query's m elements, freed with the job
    uint64_t read_ns;      // when its frame had been read
};

// The FIFO the readers fill and the W workers drain. A full queue blocks the readers, which
// pushes back on the clients through TCP.
class JobQueue {
public:
    static constexpr size_t kCapacity = 4096;

    void push(Job job) {
        std::unique_lock<std::mutex> lock(mu_);
        not_full_.wait(lock, [&] { return jobs_.size() < kCapacity; });
        jobs_.push_back(std::move(job));
        not_empty_.notify_one();
    }

    // The next job, or nullopt once the queue is closed and empty.
    std::optional<Job> pop() {
        std::unique_lock<std::mutex> lock(mu_);
        not_empty_.wait(lock, [&] { return !jobs_.empty() || closed_; });
        if (jobs_.empty()) return std::nullopt;
        Job job = std::move(jobs_.front());
        jobs_.pop_front();
        not_full_.notify_one();
        return job;
    }

    void close() {
        std::lock_guard<std::mutex> lock(mu_);
        closed_ = true;
        not_empty_.notify_all();
    }

private:
    std::mutex mu_;
    std::condition_variable not_empty_, not_full_;
    std::deque<Job> jobs_;
    bool closed_ = false;
};

// What the server's threads share. The const members are fixed before the first thread
// starts; the rest synchronise themselves.
struct Server {
    const VeriSimplePIR& pir;
    const PackedMatrix& db;
    const std::string arm;
    const std::string info;  // the INFO frame
    const int stop_fd;       // readable once SIGTERM or SIGINT has arrived
    const uint64_t start_ns;
    JobQueue queue;
    std::atomic<uint64_t> answered{0};  // ANSWER frames written
    std::atomic<uint64_t> queued{0};    // queries read and not yet started, a reader's blocked push included
    std::mutex readers_mu;
    std::condition_variable readers_done;
    size_t readers = 0;
};

constexpr uint64_t kMaxHelloBytes = 4096;  // a HELLO is {"protocol": 1}

Json::Fields stats(uint64_t start_ns, uint64_t answered, uint64_t queued) {
    rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    const auto secs = [](timeval t) { return t.tv_sec + t.tv_usec * 1e-6; };
    return {
        {"wall_s", num((now_ns() - start_ns) * 1e-9)},
        {"cpu_user_s", num(secs(ru.ru_utime))},
        {"cpu_sys_s", num(secs(ru.ru_stime))},
        {"peak_rss_bytes", Json::integer(peak_rss_bytes())},
        {"answered", Json::integer(answered)},
        {"queued", Json::integer(queued)},
    };
}

Json::Fields stats(const Server& s) { return stats(s.start_ns, s.answered, s.queued); }

// A socket closed with unread input resets the connection, and the reset discards what the
// client has not read yet. So a connection ends with FIN, and the server drops what the client
// still sends until it closes, for at most this long.
constexpr int kDrainMs = 2000;

// ERROR for the connection, then close it.
wire::Io refuse(Connection& c, const std::string& why) {
    c.send(wire::error(0, why));
    shutdown(c.fd, SHUT_WR);
    wire::drain(c.fd, kDrainMs);
    return wire::Io::Failed;
}

// After SIGTERM or SIGINT: drop what the client sends until the workers have answered every
// query this connection had read, then end the connection.
void finish(Connection& c) {
    char sink[4096];
    bool open = true;
    while (c.pending > 0) {
        if (open) {
            const wire::Io io = wire::read_some(c.fd, sink, sizeof sink, -1, 10);
            open = io == wire::Io::Ok || io == wire::Io::Timeout;
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
        }
    }
    shutdown(c.fd, SHUT_WR);
    wire::drain(c.fd, kDrainMs);
}

// A connection's reader: answers HELLO and STATS_REQ itself and queues each QUERY, until a read
// ends, and says why. A malformed query gets ERROR without reaching the library, which aborts
// the whole process on one (mat_packed.cpp:424).
wire::Io serve_connection(Server& s, const std::shared_ptr<Connection>& conn) {
    using wire::Io;
    const int fd = conn->fd;
    const uint64_t query_bytes = 8 * s.pir.m;
    for (;;) {
        uint8_t head[wire::kHeaderBytes];
        if (const Io io = wire::read_exact(fd, head, 4, s.stop_fd); io != Io::Ok) return io;
        const uint64_t length = wire::get_be(head, 4), body = length - 1;
        if (length == 0 || length > wire::kMaxLength)
            return refuse(*conn, "frame length " + std::to_string(length) + " is not between 1 and 2^30");
        if (const Io io = wire::read_exact(fd, head + 4, 1, s.stop_fd); io != Io::Ok) return io;
        const uint8_t type = head[4];
        if (type == wire::kQuery) {
            if (body < 8) return refuse(*conn, "a QUERY body starts with a u64 req_id");
            uint8_t id[8];
            if (const Io io = wire::read_exact(fd, id, sizeof id, s.stop_fd); io != Io::Ok) return io;
            const uint64_t req_id = wire::get_be(id, 8);
            if (body - 8 != query_bytes) {
                if (const Io io = wire::skip(fd, body - 8, s.stop_fd); io != Io::Ok) return io;
                conn->send(wire::error(req_id, "a query is " + std::to_string(query_bytes) + " bytes for " + s.arm +
                                                   " at these parameters, not " + std::to_string(body - 8)));
                continue;
            }
            Job job{conn, req_id, std::vector<Elem>(s.pir.m), 0};
            if (const Io io = wire::read_exact(fd, job.ct.data(), query_bytes, s.stop_fd); io != Io::Ok) return io;
            job.read_ns = now_ns();
            for (Elem& e : job.ct) e = wire::get_be(reinterpret_cast<const uint8_t*>(&e), 8);
            s.queued++;
            conn->pending++;
            s.queue.push(std::move(job));
        } else if (type == wire::kHello) {
            if (body > kMaxHelloBytes) return refuse(*conn, "HELLO is longer than " + std::to_string(kMaxHelloBytes) + " bytes");
            std::string text(body, '\0');
            if (const Io io = wire::read_exact(fd, text.data(), body, s.stop_fd); io != Io::Ok) return io;
            const std::optional<JsonValue> hello = JsonValue::parse(text);
            const JsonValue* protocol = hello ? hello->get("protocol") : nullptr;
            if (!protocol || protocol->scalar_text() != "1") return refuse(*conn, "HELLO must be {\"protocol\": 1}");
            conn->send(s.info);
        } else if (type == wire::kStatsReq) {
            if (body != 0) return refuse(*conn, "STATS_REQ has an empty body");
            conn->send(wire::frame(wire::kStats, Json::object(stats(s)).text()));
        } else {
            return refuse(*conn, "frame type " + std::to_string(type) + " is not one a client sends");
        }
    }
}

void reader(Server& s, std::shared_ptr<Connection> conn) {
    try {
        if (serve_connection(s, conn) == wire::Io::Stopped) finish(*conn);
    } catch (const std::exception& e) {
        std::fprintf(stderr, "vsp-adapter: dropped a connection: %s\n", e.what());
    }
    conn.reset();  // the socket closes now, or when the last queued query from it is answered
    std::lock_guard<std::mutex> lock(s.readers_mu);
    s.readers--;
    s.readers_done.notify_all();
}

// answer_ns spans Answer alone, as bench mode's server_answer_s does. The answer and the
// query are freed once the frame is built: Matrix has no destructor (mat.h:51-53).
void worker(Server& s) {
    while (std::optional<Job> job = s.queue.pop()) {
        s.queued--;
        Matrix ct;  // a view of the job's elements
        ct.rows = job->ct.size();
        ct.cols = 1;
        ct.data = job->ct.data();
        const uint64_t start = now_ns();
        Matrix ans = s.pir.Answer(ct, s.db);
        const uint64_t answer_ns = now_ns() - start;
        std::string frame = wire::header(wire::kAnswer, 24 + 8 * ans.rows);
        wire::put_be(frame, job->req_id, 8);
        wire::put_be(frame, start - job->read_ns, 8);
        wire::put_be(frame, answer_ns, 8);
        put_elems(frame, ans);
        release(ans);
        if (job->conn->send(frame)) s.answered++;
        job->conn->pending--;
    }
}

void accept_connections(Server& s, int listen_fd) {
    for (;;) {
        pollfd fds[2] = {{listen_fd, POLLIN, 0}, {s.stop_fd, POLLIN, 0}};
        if (poll(fds, 2, -1) < 0) {
            if (errno == EINTR) continue;
            throw std::runtime_error(std::string("poll failed: ") + std::strerror(errno));
        }
        if (fds[1].revents) return;
        const int fd = accept(listen_fd, nullptr, nullptr);
        if (fd < 0) {
            if (errno == EMFILE || errno == ENFILE || errno == ENOBUFS || errno == ENOMEM) poll(&fds[1], 1, 100);
            continue;
        }
        wire::no_delay(fd);
        {
            std::lock_guard<std::mutex> lock(s.readers_mu);
            s.readers++;
        }
        try {
            std::thread(reader, std::ref(s), std::make_shared<Connection>(fd)).detach();
        } catch (const std::system_error& e) {
            std::fprintf(stderr, "vsp-adapter: dropped a connection: %s\n", e.what());
            std::lock_guard<std::mutex> lock(s.readers_mu);
            s.readers--;
        }
    }
}

// The summary record: "mode": "serve", the INFO fields but scheme_params, then the final STATS.
Json::Fields serve_summary(const Json::Fields& info, const Json::Fields& final_stats) {
    Json::Fields f = {{"mode", Json::string("serve")}};
    f.insert(f.end(), info.begin(), info.end());
    f.insert(f.end(), final_stats.begin(), final_stats.end());
    return f;
}

// SIGTERM and SIGINT stop the server: a sigwait thread writes to the stop pipe, the accept
// loop and the readers see it and stop reading, the workers answer every query already read,
// and the summary record goes to --out.
int run_serve(const Options& o, const Layout& l, std::optional<Sink>& sink) {
    const uint64_t start_ns = now_ns();
    sigset_t stop_signals;
    sigemptyset(&stop_signals);
    sigaddset(&stop_signals, SIGTERM);
    sigaddset(&stop_signals, SIGINT);
    pthread_sigmask(SIG_BLOCK, &stop_signals, nullptr);  // before any thread starts, so all inherit it
    std::signal(SIGPIPE, SIG_IGN);

    const ArmSpec& arm = *o.arm;
    const auto pir = make_pir(arm, l);
    const std::vector<uint8_t> bytes = database_bytes(o.db_bytes, o.seed);
    const ServerDB db(*pir, arm, l, bytes, false);
    const PublicSeeds seeds(o.seed);
    const std::unique_ptr<ServerHint> hint = server_offline(*pir, arm, db, seeds);
    const Json params = scheme_params(arm, o, l, pir.get());

    const Json::Fields info = info_fields(o, bytes, seeds);
    Json::Fields info_frame = info;
    info_frame.emplace_back("scheme_params", params);

    int stop_pipe[2];
    if (pipe(stop_pipe)) throw std::runtime_error("pipe failed");
    uint16_t port = 0;
    const int listen_fd = wire::listen_on(*o.listen, port);
    Server s{*pir, *db.D_packed, arm.name, wire::frame(wire::kInfo, Json::object(info_frame).text()), stop_pipe[0],
             start_ns};

    std::vector<std::thread> workers;
    for (uint64_t i = 0; i < o.workers; i++) workers.emplace_back(worker, std::ref(s));
    std::thread([stop_signals, fd = stop_pipe[1]] {
        int sig = 0;
        sigwait(&stop_signals, &sig);
        if (write(fd, "x", 1) != 1) std::abort();
    }).detach();

    std::cout.flush();
    std::fprintf(stderr, "vpir-serve ready %s\n", wire::Endpoint{o.listen->host, std::to_string(port)}.text().c_str());
    accept_connections(s, listen_fd);
    close(listen_fd);
    {
        std::unique_lock<std::mutex> lock(s.readers_mu);
        s.readers_done.wait(lock, [&] { return s.readers == 0; });
    }
    s.queue.close();
    for (std::thread& t : workers) t.join();
    std::fprintf(stderr, "%s: served %llu queries\n", arm.name, (unsigned long long)s.answered.load());
    if (sink) sink->write(Json::object(serve_summary(info, stats(s))).text());
    return 0;
}

// ------------------------------------------------ the client

// Upstream rejects an answer with assert, which aborts. The client turns that into exit 1 with
// this note, so a rejected answer is a clear failure rather than a crash, and verification
// runs in this process, timed as bench mode times an honest repetition's.
char g_abort_note[256] = "upstream aborted";

extern "C" void client_abort(int) {
    static const char prefix[] = "vsp-adapter: ";
    write(STDERR_FILENO, prefix, sizeof prefix - 1);
    write(STDERR_FILENO, g_abort_note, std::strlen(g_abort_note));
    write(STDERR_FILENO, "\n", 1);
    _exit(1);
}

struct Frame {
    uint8_t type;
    std::string body;
};

Frame read_frame(int fd) {
    uint8_t head[wire::kHeaderBytes];
    if (wire::read_exact(fd, head, sizeof head, -1) != wire::Io::Ok)
        throw std::runtime_error("the server closed the connection");
    const uint64_t length = wire::get_be(head, 4);
    if (length == 0 || length > wire::kMaxLength)
        throw std::runtime_error("the server sent a frame of length " + std::to_string(length));
    Frame f{head[4], std::string(length - 1, '\0')};
    if (wire::read_exact(fd, f.body.data(), f.body.size(), -1) != wire::Io::Ok)
        throw std::runtime_error("the server closed the connection inside a frame");
    return f;
}

// INFO's fingerprint must match the client's own state.
void check_info(const Frame& info, const Json::Fields& mine) {
    const std::optional<JsonValue> got = info.type == wire::kInfo ? JsonValue::parse(info.body) : std::nullopt;
    if (!got || got->kind != JsonValue::Object) throw std::runtime_error("the server's reply to HELLO is not an INFO object");
    std::string diff;
    for (const auto& [key, want] : mine) {
        if (std::find(std::begin(kFingerprint), std::end(kFingerprint), key) == std::end(kFingerprint)) continue;
        const JsonValue* v = got->get(key);
        const std::string have = v ? v->scalar_text() : "missing";
        if (have != want.text()) diff += " " + key + " is " + have + ", not " + want.text() + ";";
    }
    if (!diff.empty()) throw std::runtime_error("the server's INFO differs from the client's state:" + diff);
}

// The corpus a load generator replays (docs/serve_protocol.md, "Corpus"). It is written to
// PATH.partial and renamed to PATH after the last verified answer, so PATH only ever holds a
// complete corpus.
class Corpus {
public:
    Corpus(const std::string& path, const Frame& info)
        : path_(path), partial_(path + ".partial"), f_(std::fopen(partial_.c_str(), "wb")) {
        if (!f_) throw std::runtime_error("cannot open " + partial_ + ": " + std::strerror(errno));
        put(wire::frame(wire::kInfo, info.body));
    }
    ~Corpus() {
        if (f_) std::fclose(f_);
    }
    Corpus(const Corpus&) = delete;

    void add(uint64_t req_id, const std::string& query_frame, const std::string& answer) {
        put(query_frame);
        std::string expect;
        wire::put_be(expect, req_id, 8);
        wire::put_be(expect, 0, 1);  // server_id
        wire::put_be(expect, answer.size(), 4);
        put(wire::frame(wire::kExpect, expect + sha256(answer.data(), answer.size())));
    }

    void finish() {
        const bool closed = std::fclose(f_) == 0;
        f_ = nullptr;
        if (!closed || std::rename(partial_.c_str(), path_.c_str()))
            throw std::runtime_error("cannot write " + path_ + ": " + std::strerror(errno));
    }

private:
    void put(const std::string& bytes) {
        if (std::fwrite(bytes.data(), 1, bytes.size(), f_) != bytes.size())
            throw std::runtime_error("write to " + partial_ + " failed");
    }
    std::string path_, partial_;
    FILE* f_;
};

// A real client of one server. It rebuilds the offline state from --seed, untimed (S1 and S2
// run the whole preprocessing exchange against a local server object over the same database),
// then sends Q queries one at a time and verifies and decodes each answer. Any rejected or
// wrongly decoded answer ends the run with exit 1.
int run_client(const Options& o, const Layout& l, Sink& sink) {
    std::signal(SIGABRT, client_abort);
    std::signal(SIGPIPE, SIG_IGN);
    const ArmSpec& arm = *o.arm;
    const auto pir = make_pir(arm, l);
    const std::vector<uint8_t> bytes = database_bytes(o.db_bytes, o.seed);
    const ServerDB db(*pir, arm, l, bytes, false);
    const PublicSeeds seeds(o.seed);
    std::snprintf(g_abort_note, sizeof g_abort_note, "upstream aborted in the client's offline phase");
    const std::unique_ptr<ClientState> state = run_offline(*pir, arm, db, std::nullopt, &seeds).state;
    std::snprintf(g_abort_note, sizeof g_abort_note, "upstream aborted");
    const Json params = scheme_params(arm, o, l, pir.get()), sizes = analytical(arm, pir->dbParams);
    const WireBits w = wire_bits(*pir);

    const int fd = wire::connect_to(*o.connect);
    if (!wire::write_all(fd, wire::frame(wire::kHello, Json::object({{"protocol", Json::integer(1)}}).text())))
        throw std::runtime_error("the server closed the connection");
    const Frame info = read_frame(fd);
    check_info(info, info_fields(o, bytes, seeds));
    std::optional<Corpus> corpus;
    if (!o.corpus_out.empty()) corpus.emplace(o.corpus_out, info);

    Stream index_stream(o.seed, kIndex);
    for (uint64_t q = 0; q < o.reps; q++) {
        const uint64_t req_id = q + 1;  // ERROR reserves req_id 0 for the connection
        const uint64_t record = index_stream.below(l.records);
        QueryResult res;
        std::pair<Matrix, Matrix> ct_sk;
        timed(res.query_gen, [&] { ct_sk = pir->Query(state->A, l.entry(record, 0)); });
        Matrix& ct = ct_sk.first;
        Matrix& sk = ct_sk.second;
        res.up_bytes = wire_bytes(ct, w.q);
        std::string query = wire::header(wire::kQuery, 8 + 8 * ct.rows);
        wire::put_be(query, req_id, 8);
        put_elems(query, ct);

        Frame reply{};
        timed(res.rtt, [&] {
            if (!wire::write_all(fd, query)) throw std::runtime_error("the server closed the connection");
            reply = read_frame(fd);
        });
        const auto* p = reinterpret_cast<const uint8_t*>(reply.body.data());
        if (reply.type == wire::kError && reply.body.size() >= 8)
            throw std::runtime_error("the server refused req_id " + std::to_string(wire::get_be(p, 8)) + ": " +
                                     reply.body.substr(8));
        if (reply.type != wire::kAnswer || reply.body.size() != 24 + 8 * pir->ell || wire::get_be(p, 8) != req_id)
            throw std::runtime_error("the reply to req_id " + std::to_string(req_id) + " is not its " +
                                     std::to_string(8 * pir->ell) + "-byte ANSWER");
        res.answer.wall = wire::get_be(p + 16, 8) * 1e-9;
        Matrix ans(pir->ell, 1);
        get_elems(p + 24, ans);
        res.down_bytes = wire_bytes(ans, w.q);

        if (arm.verifiable()) {
            std::snprintf(g_abort_note, sizeof g_abort_note,
                          "the client rejected the answer to req_id %llu: PreVerify failed", (unsigned long long)req_id);
            timed(res.verify.emplace(), [&] { pir->PreVerify(ct, ans, state->Z, *state->C); });
            std::snprintf(g_abort_note, sizeof g_abort_note, "upstream aborted");
            res.detected = false;
        }
        std::vector<uint64_t> got;
        timed(res.recover.emplace(), [&] { got = decode_record(*pir, l, state->H, ans, sk, record); });
        res.correct = got == truth_of(l, bytes, record);

        Record r{o, params};
        r.phase = Phase::Online;
        r.rep = q;
        r.recovery = Recovery{*res.correct ? Outcome::Accepted : Outcome::SilentWrong};
        r.metrics = metrics(Phase::Online, nullptr, &res, &*r.recovery, Mode::Client);
        r.analytical = sizes;
        r.correct = res.correct;
        r.detected = res.detected;
        sink.write(r);
        if (!*res.correct)
            throw std::runtime_error("the answer to req_id " + std::to_string(req_id) + " decoded to the wrong record");
        if (corpus) corpus->add(req_id, query, reply.body.substr(24));
        release(ct);
        release(sk);
        release(ans);
    }
    if (corpus) corpus->finish();
    close(fd);
    std::fprintf(stderr, "%s: %llu client queries written\n", arm.name, (unsigned long long)o.reps);
    return 0;
}

// Parameter selection runs in a child, as in every mode. Unsupported parameters are a result:
// bench and client mode write their records with status unsupported_params, and serve mode
// writes its summary record so, without listening.
int run(const Options& o) {
    std::optional<Sink> sink;
    if (!o.out.empty()) sink.emplace(o.out);
    const ArmSpec& arm = *o.arm;
    auto [layout, why] = select_params(arm, o.db_bytes, o.record_bytes);
    if (!layout) {
        // Upstream prints progress before its message, so keep the last lines: the message and the assert.
        size_t cut = why.size();
        for (int lines = 0; lines < 3 && cut != std::string::npos && cut > 0; lines++) cut = why.rfind('\n', cut - 1);
        if (cut != std::string::npos && cut < why.size()) why = why.substr(cut + 1);
        std::fprintf(stderr, "%s: unsupported parameters: %s\n", arm.name, why.c_str());
        if (o.mode == Mode::Serve) {
            Json::Fields f = serve_summary(info_fields(o, database_bytes(o.db_bytes, o.seed), PublicSeeds(o.seed)),
                                           stats(now_ns(), 0, 0));
            f.emplace_back("status", Json::string("unsupported_params"));
            f.emplace_back("error", Json::string(why));
            if (sink) sink->write(Json::object(f).text());
        } else {
            write_unsupported(o, why, *sink);
        }
        return 0;
    }
    switch (o.mode) {
        case Mode::Serve: return run_serve(o, *layout, sink);
        case Mode::Client: return run_client(o, *layout, *sink);
        case Mode::Bench: break;
    }
    return run_bench(o, *layout, *sink);
}

}  // namespace

int main(int argc, char** argv) {
    for (int i = 1; i < argc; i++) {
        if (!std::strcmp(argv[i], "--help") || !std::strcmp(argv[i], "-h")) {
            std::fputs(kUsage, stdout);
            return 0;
        }
    }
    Options o;
    try {
        o = parse_args(argc, argv);
    } catch (const UsageError& e) {
        std::fprintf(stderr, "vsp-adapter: %s\n\n%s", e.what(), kUsage);
        return 2;
    }
    dup2(STDERR_FILENO, STDOUT_FILENO);  // upstream prints progress on stdout; keep stdout clean
    try {
        return run(o);
    } catch (const std::exception& e) {
        std::cout.flush();  // upstream's buffered progress first, so the reason is the last line
        std::fprintf(stderr, "vsp-adapter: %s\n", e.what());
        return 1;
    }
}
