#pragma once

// The serve protocol's frames and sockets (docs/serve_protocol.md, "Framing").

#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <optional>
#include <poll.h>
#include <stdexcept>
#include <string>
#include <sys/socket.h>
#include <unistd.h>

namespace wire {

enum Type : uint8_t {
    kHello = 0x01,
    kInfo = 0x02,
    kQuery = 0x03,
    kAnswer = 0x04,
    kStatsReq = 0x05,
    kStats = 0x06,
    kError = 0x07,
    kExpect = 0x10,
};

constexpr uint64_t kMaxLength = 1ULL << 30;  // a peer refuses a longer frame
constexpr size_t kHeaderBytes = 5;           // u32 length, u8 type

inline void put_be(std::string& out, uint64_t v, int bytes) {
    for (int i = bytes - 1; i >= 0; i--) out += char(v >> (8 * i));
}

inline uint64_t get_be(const uint8_t* p, int bytes) {
    uint64_t v = 0;
    for (int i = 0; i < bytes; i++) v = v << 8 | p[i];
    return v;
}

// The first bytes of a frame whose body is body_bytes long.
inline std::string header(Type t, uint64_t body_bytes) {
    std::string h;
    put_be(h, 1 + body_bytes, 4);
    h += char(t);
    return h;
}

inline std::string frame(Type t, const std::string& body) { return header(t, body.size()) + body; }

inline std::string error(uint64_t req_id, const std::string& message) {
    std::string body;
    put_be(body, req_id, 8);
    return frame(kError, body + message);
}

enum class Io { Ok, Eof, Stopped, Timeout, Failed };

// Reads at least one and at most n bytes, and says how many in got. Gives up with Stopped once
// stop_fd (when not -1) is readable, and with Timeout if timeout_ms (when not -1) passes first.
inline Io read_some(int fd, void* buf, size_t n, int stop_fd, int timeout_ms = -1, size_t* got = nullptr) {
    for (;;) {
        pollfd fds[2] = {{fd, POLLIN, 0}, {stop_fd, POLLIN, 0}};
        const int ready = poll(fds, stop_fd < 0 ? 1 : 2, timeout_ms);
        if (ready < 0 && errno == EINTR) continue;
        if (ready < 0) return Io::Failed;
        if (ready == 0) return Io::Timeout;
        if (stop_fd >= 0 && fds[1].revents) return Io::Stopped;
        const ssize_t k = recv(fd, buf, n, 0);
        if (k > 0) {
            if (got) *got = k;
            return Io::Ok;
        }
        if (k == 0) return Io::Eof;
        if (errno != EINTR && errno != EAGAIN) return Io::Failed;
    }
}

inline Io read_exact(int fd, void* buf, size_t n, int stop_fd) {
    auto* p = static_cast<uint8_t*>(buf);
    while (n > 0) {
        size_t got = 0;
        if (const Io io = read_some(fd, p, n, stop_fd, -1, &got); io != Io::Ok) return io;
        p += got;
        n -= got;
    }
    return Io::Ok;
}

// Reads and drops input until the peer closes or deadline_ms passes.
inline void drain(int fd, int deadline_ms) {
    char buf[4096];
    const auto end = std::chrono::steady_clock::now() + std::chrono::milliseconds(deadline_ms);
    for (;;) {
        const auto left = std::chrono::duration_cast<std::chrono::milliseconds>(end - std::chrono::steady_clock::now());
        if (left.count() <= 0 || read_some(fd, buf, sizeof buf, -1, int(left.count())) != Io::Ok) return;
    }
}

// Reads and drops n bytes.
inline Io skip(int fd, uint64_t n, int stop_fd) {
    char buf[4096];
    while (n > 0) {
        size_t got = 0;
        if (const Io io = read_some(fd, buf, n < sizeof buf ? n : sizeof buf, stop_fd, -1, &got); io != Io::Ok)
            return io;
        n -= got;
    }
    return Io::Ok;
}

inline bool write_all(int fd, const std::string& bytes) {
    const char* p = bytes.data();
    size_t n = bytes.size();
    while (n > 0) {
        const ssize_t k = send(fd, p, n, 0);
        if (k < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        p += k;
        n -= k;
    }
    return true;
}

// HOST:PORT, with an IPv6 host in brackets.
struct Endpoint {
    std::string host, port;
    std::string text() const { return (host.find(':') == std::string::npos ? host : "[" + host + "]") + ":" + port; }
};

inline std::optional<Endpoint> parse_endpoint(const std::string& s) {
    const size_t colon = s.rfind(':');
    if (colon == std::string::npos || colon == 0 || colon + 1 == s.size()) return std::nullopt;
    std::string host = s.substr(0, colon), port = s.substr(colon + 1);
    if (host.front() == '[') {
        if (host.size() < 3 || host.back() != ']') return std::nullopt;
        host = host.substr(1, host.size() - 2);
    }
    if (port.size() > 5 || port.find_first_not_of("0123456789") != std::string::npos || std::stoul(port) > 65535)
        return std::nullopt;
    return Endpoint{host, port};
}

inline addrinfo* resolve(const Endpoint& e, bool passive) {
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_flags = AI_NUMERICSERV | (passive ? AI_PASSIVE : 0);
    addrinfo* res = nullptr;
    if (const int rc = getaddrinfo(e.host.c_str(), e.port.c_str(), &hints, &res))
        throw std::runtime_error("cannot resolve " + e.text() + ": " + gai_strerror(rc));
    return res;
}

inline void no_delay(int fd) {
    const int one = 1;
    setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof one);
}

// A listening socket, and the port it got (the real one when e.port is 0).
inline int listen_on(const Endpoint& e, uint16_t& port) {
    addrinfo* res = resolve(e, true);
    const int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    const int one = 1;
    const bool ok = fd >= 0 && !setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one) &&
                    !bind(fd, res->ai_addr, res->ai_addrlen) && !listen(fd, SOMAXCONN);
    const int err = errno;
    freeaddrinfo(res);
    if (!ok) {
        if (fd >= 0) close(fd);
        throw std::runtime_error("cannot listen on " + e.text() + ": " + std::strerror(err));
    }
    sockaddr_storage addr{};
    socklen_t len = sizeof addr;
    getsockname(fd, reinterpret_cast<sockaddr*>(&addr), &len);
    port = ntohs(addr.ss_family == AF_INET6 ? reinterpret_cast<sockaddr_in6*>(&addr)->sin6_port
                                            : reinterpret_cast<sockaddr_in*>(&addr)->sin_port);
    return fd;
}

inline int connect_to(const Endpoint& e) {
    addrinfo* res = resolve(e, false);
    const int fd = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
    const bool ok = fd >= 0 && !connect(fd, res->ai_addr, res->ai_addrlen);
    const int err = errno;
    freeaddrinfo(res);
    if (!ok) {
        if (fd >= 0) close(fd);
        throw std::runtime_error("cannot connect to " + e.text() + ": " + std::strerror(err));
    }
    no_delay(fd);
    return fd;
}

}  // namespace wire
