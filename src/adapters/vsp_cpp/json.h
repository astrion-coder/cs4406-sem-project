#pragma once

#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

// A rendered JSON value. Built bottom-up, so a finished Json is always valid text.
class Json {
public:
    using Fields = std::vector<std::pair<std::string, Json>>;

    static Json null() { return Json("null"); }
    static Json boolean(bool b) { return Json(b ? "true" : "false"); }
    static Json integer(uint64_t v) { return Json(std::to_string(v)); }

    static Json number(double v) {
        if (!std::isfinite(v)) return null();
        char buf[32];
        std::snprintf(buf, sizeof buf, "%.17g", v);
        return Json(buf);
    }

    static Json string(std::string_view s) {
        std::string out = "\"";
        for (unsigned char c : s) {
            switch (c) {
                case '"': out += "\\\""; break;
                case '\\': out += "\\\\"; break;
                case '\b': out += "\\b"; break;
                case '\f': out += "\\f"; break;
                case '\n': out += "\\n"; break;
                case '\r': out += "\\r"; break;
                case '\t': out += "\\t"; break;
                default:
                    if (c < 0x20) {
                        char buf[8];
                        std::snprintf(buf, sizeof buf, "\\u%04x", c);
                        out += buf;
                    } else {
                        out += static_cast<char>(c);
                    }
            }
        }
        return Json(out + "\"");
    }

    static Json list(const std::vector<Json>& items) {
        std::string out = "[";
        for (size_t i = 0; i < items.size(); i++) out += (i ? "," : "") + items[i].text_;
        return Json(out + "]");
    }

    static Json object(const Fields& fields) {
        std::string out = "{";
        for (size_t i = 0; i < fields.size(); i++)
            out += (i ? "," : "") + string(fields[i].first).text_ + ":" + fields[i].second.text_;
        return Json(out + "}");
    }

    template <class T, class F>
    static Json optional(const std::optional<T>& v, F make) { return v ? make(*v) : null(); }

    const std::string& text() const { return text_; }

private:
    explicit Json(std::string text) : text_(std::move(text)) {}
    std::string text_;
};

// A parsed JSON value, for the serve protocol's HELLO and INFO bodies.
struct JsonValue {
    enum Kind { Null, Bool, Number, String, Array, Object } kind = Null;
    std::string text;  // a string's decoded value, or a number's or boolean's source text
    std::vector<JsonValue> items;
    std::vector<std::pair<std::string, JsonValue>> fields;

    const JsonValue* get(std::string_view key) const {
        for (const auto& [k, v] : fields)
            if (k == key) return &v;
        return nullptr;
    }

    // A scalar as Json renders it, so it compares with a Json the adapter built.
    std::string scalar_text() const {
        if (kind == String) return Json::string(text).text();
        return kind == Null ? "null" : text;
    }

    // nullopt unless s is one well-formed value. Nesting is capped, because a peer's
    // message must not exhaust a reader thread's stack.
    static std::optional<JsonValue> parse(std::string_view s) {
        Parser p{s};
        JsonValue v;
        if (!p.value(v, 0)) return std::nullopt;
        p.space();
        if (p.i != s.size()) return std::nullopt;
        return v;
    }

private:
    struct Parser {
        std::string_view s;
        size_t i = 0;
        static constexpr int kMaxDepth = 16;

        bool more() const { return i < s.size(); }
        void space() {
            while (more() && (s[i] == ' ' || s[i] == '\t' || s[i] == '\n' || s[i] == '\r')) i++;
        }
        bool eat(char c) {
            space();
            if (!more() || s[i] != c) return false;
            i++;
            return true;
        }
        bool word(std::string_view w) {
            if (s.substr(i, w.size()) != w) return false;
            i += w.size();
            return true;
        }
        static int hex(char c) {
            if (c >= '0' && c <= '9') return c - '0';
            if (c >= 'a' && c <= 'f') return c - 'a' + 10;
            if (c >= 'A' && c <= 'F') return c - 'A' + 10;
            return -1;
        }
        bool string(std::string& out) {
            if (!eat('"')) return false;
            while (more()) {
                const char c = s[i++];
                if (c == '"') return true;
                if (c != '\\') {
                    out += c;
                    continue;
                }
                if (!more()) return false;
                const char e = s[i++];
                switch (e) {
                    case '"': case '\\': case '/': out += e; break;
                    case 'b': out += '\b'; break;
                    case 'f': out += '\f'; break;
                    case 'n': out += '\n'; break;
                    case 'r': out += '\r'; break;
                    case 't': out += '\t'; break;
                    case 'u': {
                        unsigned cp = 0;
                        for (int k = 0; k < 4; k++) {
                            if (!more() || hex(s[i]) < 0) return false;
                            cp = cp << 4 | hex(s[i++]);
                        }
                        if (cp < 0x80) {
                            out += char(cp);
                        } else if (cp < 0x800) {
                            out += char(0xC0 | cp >> 6);
                            out += char(0x80 | (cp & 0x3F));
                        } else {
                            out += char(0xE0 | cp >> 12);
                            out += char(0x80 | (cp >> 6 & 0x3F));
                            out += char(0x80 | (cp & 0x3F));
                        }
                        break;
                    }
                    default: return false;
                }
            }
            return false;
        }
        bool value(JsonValue& v, int depth) {
            space();
            if (!more() || depth > kMaxDepth) return false;
            const char c = s[i];
            if (c == '{') {
                v.kind = Object;
                i++;
                if (eat('}')) return true;
                do {
                    std::string key;
                    JsonValue item;
                    if (!string(key) || !eat(':') || !value(item, depth + 1)) return false;
                    v.fields.emplace_back(std::move(key), std::move(item));
                } while (eat(','));
                return eat('}');
            }
            if (c == '[') {
                v.kind = Array;
                i++;
                if (eat(']')) return true;
                do {
                    JsonValue item;
                    if (!value(item, depth + 1)) return false;
                    v.items.push_back(std::move(item));
                } while (eat(','));
                return eat(']');
            }
            if (c == '"') {
                v.kind = String;
                return string(v.text);
            }
            for (const char* w : {"null", "true", "false"}) {
                if (word(w)) {
                    v.kind = w[0] == 'n' ? Null : Bool;
                    v.text = w;
                    return true;
                }
            }
            const size_t start = i;
            while (more() && s[i] && (std::isdigit(static_cast<unsigned char>(s[i])) || std::strchr("+-.eE", s[i]))) i++;
            if (i == start) return false;
            v.kind = Number;
            v.text = std::string(s.substr(start, i - start));
            return true;
        }
    };
};
