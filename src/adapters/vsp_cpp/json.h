#pragma once

#include <cmath>
#include <cstdint>
#include <cstdio>
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
