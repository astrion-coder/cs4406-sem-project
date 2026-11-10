// VeriSimplePIR adapter: arms S0-cpp, S1 and S2 (src/adapters/README.md).

#include "pir/preproc_pir.h"

#include <cerrno>
#include <csignal>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <memory>
#include <new>
#include <type_traits>
#include <stdexcept>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

#include "json.h"

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

struct Options {
    const ArmSpec* arm = nullptr;
    uint64_t db_bytes = 0, record_bytes = 0;
    bool offline = false, online = false;
    uint64_t reps = 0, warmup = 0, seed = 0;
    const FaultSpec* fault = &kFaults[0];
    double fault_prob = 0.0;
    const PolicySpec* recovery = &kPolicies[0];
    bool replicas_cheat = false;
    std::string out;
};

constexpr const char* kUsage =
    "usage: vsp-adapter --arm S0-cpp|S1|S2 --db-bytes N --record-bytes N\n"
    "                   --phase offline|online|all --reps N --warmup N --seed N\n"
    "                   --fault none|flip_answer_bit|wrong_record|flip_hint_bit|\n"
    "                           stale_record|drop_server|bad_proof\n"
    "                   [--fault-prob F] [--recovery abort|switch_replica|re_preprocess]\n"
    "                   [--replicas-cheat] --out PATH.jsonl\n"
    "\n"
    "Drives VeriSimplePIR (third_party/verisimplepir) and appends one JSON record per\n"
    "repetition and phase to --out. Contract: src/adapters/README.md.\n"
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

Options parse_args(int argc, char** argv) {
    Options o;
    bool have_arm = false, have_db = false, have_record = false, have_phase = false;
    bool have_reps = false, have_warmup = false, have_seed = false, have_prob = false;
    for (int i = 1; i < argc; i++) {
        const std::string flag = argv[i];
        if (flag == "--replicas-cheat") {
            o.replicas_cheat = true;
            continue;
        }
        if (i + 1 >= argc) throw UsageError(flag + " needs a value");
        const std::string v = argv[++i];
        if (flag == "--arm") {
            for (const auto& a : kArms)
                if (v == a.name) o.arm = &a;
            if (!o.arm) throw UsageError("this adapter serves S0-cpp, S1 and S2, not '" + v + "'");
            have_arm = true;
        } else if (flag == "--db-bytes") {
            o.db_bytes = parse_u64(flag, v), have_db = true;
        } else if (flag == "--record-bytes") {
            o.record_bytes = parse_u64(flag, v), have_record = true;
        } else if (flag == "--phase") {
            o.offline = v == "offline" || v == "all";
            o.online = v == "online" || v == "all";
            if (!o.offline && !o.online) throw UsageError("--phase must be offline, online or all");
            have_phase = true;
        } else if (flag == "--reps") {
            o.reps = parse_u64(flag, v), have_reps = true;
        } else if (flag == "--warmup") {
            o.warmup = parse_u64(flag, v), have_warmup = true;
        } else if (flag == "--seed") {
            o.seed = parse_u64(flag, v), have_seed = true;
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
        } else if (flag == "--out") {
            o.out = v;
        } else {
            throw UsageError("unknown flag " + flag);
        }
    }
    if (!have_arm || !have_db || !have_record || !have_phase || !have_reps || !have_warmup || !have_seed || o.out.empty())
        throw UsageError("--arm, --db-bytes, --record-bytes, --phase, --reps, --warmup, --seed and --out are required");
    if (o.record_bytes == 0) throw UsageError("--record-bytes must be positive for " + std::string(o.arm->name));
    if (o.db_bytes == 0 || o.db_bytes % o.record_bytes)
        throw UsageError("--db-bytes must be a positive multiple of --record-bytes");
    if (!(o.arm->faults & o.fault->kind))
        throw UsageError(std::string(o.arm->name) + " does not support --fault " + o.fault->name);
    if (o.recovery->kind != Policy::Abort && !o.arm->verifiable())
        throw UsageError(std::string(o.arm->name) + " never rejects an answer, so it supports only --recovery abort");
    if (o.fault->kind == kWrongRecord && o.db_bytes / o.record_bytes < 2)
        throw UsageError("wrong_record needs at least two records");
    if (!have_prob) o.fault_prob = o.fault->kind == kNone ? 0.0 : 1.0;
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

enum Purpose : uint64_t { kDatabase = 1, kIndex = 2, kFault = 3 };

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
                          std::optional<OfflineFlip> flip) {
    OfflineResult out;
    OfflineMetrics& r = out.metrics;
    out.state = std::make_unique<ClientState>();
    ClientState& c = *out.state;
    const WireBits w = wire_bits(pir);

    timed(r.public_params, [&] { c.A = pir.Init(); });
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
    timed(r.public_params, [&] { A2 = pir.PreprocInit(); });
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

// A timed offline repetition, run in a forked child. Each S1/S2 offline phase leaks tens of
// MB inside the library (multilimb_lhe.cpp:14-17; Matrix has no destructor), and the leak
// dies with the child. The peak RSS is the child's.
OfflineMetrics run_offline_isolated(const VeriSimplePIR& pir, const ArmSpec& arm, const ServerDB& db,
                                    std::optional<OfflineFlip> flip) {
    auto [m, output] = run_isolated_value<OfflineMetrics>([&] {
        OfflineMetrics r = run_offline(pir, arm, db, flip).metrics;
        r.peak_rss_bytes = peak_rss_bytes();
        return r;
    });
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
    Span query_gen, answer;
    std::optional<Span> verify, recover;
    uint64_t up_bytes = 0, down_bytes = 0;
    std::optional<bool> correct, detected;

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
// online record's is this process's.
Json metrics(Phase phase, const OfflineMetrics* off, const QueryResult* on, const Recovery* rec) {
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
    f.emplace_back("server_answer_cpu_s", one(on, num(on ? on->answer.cpu : 0)));
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

// One record. Run-wide fields are fixed at construction; each repetition fills the rest.
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
        return Json::object({
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
        }).text();
    }
};

class Sink {
public:
    explicit Sink(const std::string& path) : f_(std::fopen(path.c_str(), "a")) {
        if (!f_) throw std::runtime_error("cannot open --out " + path + ": " + std::strerror(errno));
    }
    ~Sink() { std::fclose(f_); }
    void write(const Record& r) {
        const std::string line = r.to_json() + "\n";
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
            r.metrics = metrics(phase, nullptr, nullptr, nullptr);
            r.error = why;
            sink.write(r);
        }
    }
}

int run(const Options& o) {
    Sink sink(o.out);
    const ArmSpec& arm = *o.arm;
    const uint64_t total = o.warmup + o.reps;

    auto [layout, why] = select_params(arm, o.db_bytes, o.record_bytes);
    if (!layout) {
        // Upstream prints progress before its message, so keep the last lines: the message and the assert.
        size_t cut = why.size();
        for (int lines = 0; lines < 3 && cut != std::string::npos && cut > 0; lines++) cut = why.rfind('\n', cut - 1);
        if (cut != std::string::npos && cut < why.size()) why = why.substr(cut + 1);
        std::fprintf(stderr, "%s: unsupported parameters: %s\n", arm.name, why.c_str());
        write_unsupported(o, why, sink);
        return 0;
    }
    const Layout& l = *layout;
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
            const OfflineMetrics m = run_offline_isolated(*pir, arm, db, flip);
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
        std::fprintf(stderr, "vsp-adapter: %s\n", e.what());
        return 1;
    }
}
