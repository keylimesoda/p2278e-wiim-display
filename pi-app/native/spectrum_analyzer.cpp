// Native 9-band spectrum analyzer for the Adafruit P2278E display.
//
// Input:  mono float32 PCM on stdin at --sample-rate (default 48000 Hz)
// Output: compact JSON { sample_rate, hop_seconds, block_size, mode, bands, frames }
//
// DSP shape: 2048-sample Hann window, 800-sample hop, KissFFT, octave-ish
// bands, per-band dB calibration, attack/release smoothing. Python only
// orchestrates decode/cache/render; the FFT work stays native.

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <string>
#include <vector>

extern "C" {
#include "kiss_fft.h"
}

namespace {

constexpr double kPi = 3.141592653589793238462643383279502884;
constexpr double kEpsilon = 1e-20;
constexpr const char* kMode = "native_spectrum_fft_v2_octave2048_calibrated";

struct Args {
    int sample_rate = 48000;
    int window = 2048;
    int hop = 800;
    double attack = 0.68;
    double release = 0.14;
};

struct Band {
    double low_hz;
    double high_hz;
};

const Band kBands[9] = {
    {40.0, 80.0},
    {80.0, 160.0},
    {160.0, 320.0},
    {320.0, 640.0},
    {640.0, 1250.0},
    {1250.0, 2500.0},
    {2500.0, 5000.0},
    {5000.0, 10000.0},
    {10000.0, 16000.0},
};

// Post-normalization visual calibration. Keep close to 1.0; per-band percentile
// scaling does the heavy lifting, and these are just final display taste knobs.
const double kBandGain[9] = {0.92, 0.94, 0.98, 1.00, 1.00, 1.04, 1.08, 1.12, 1.16};

void usage(const char* argv0) {
    std::cerr << "usage: " << argv0
              << " [--sample-rate 48000] [--window 2048] [--hop 800]"
              << " [--attack 0.68] [--release 0.14]\n";
}

bool parse_int(const char* s, int* out) {
    char* end = nullptr;
    long v = std::strtol(s, &end, 10);
    if (!end || *end || v <= 0 || v > 1000000) return false;
    *out = static_cast<int>(v);
    return true;
}

bool parse_double(const char* s, double* out) {
    char* end = nullptr;
    double v = std::strtod(s, &end);
    if (!end || *end || !std::isfinite(v)) return false;
    *out = v;
    return true;
}

Args parse_args(int argc, char** argv) {
    Args args;
    for (int i = 1; i < argc; ++i) {
        std::string key(argv[i]);
        auto need = [&](const char* name) -> const char* {
            if (i + 1 >= argc) {
                std::cerr << "missing value for " << name << "\n";
                usage(argv[0]);
                std::exit(2);
            }
            return argv[++i];
        };
        if (key == "--sample-rate") {
            if (!parse_int(need("--sample-rate"), &args.sample_rate)) std::exit(2);
        } else if (key == "--window") {
            if (!parse_int(need("--window"), &args.window)) std::exit(2);
        } else if (key == "--hop") {
            if (!parse_int(need("--hop"), &args.hop)) std::exit(2);
        } else if (key == "--attack") {
            if (!parse_double(need("--attack"), &args.attack)) std::exit(2);
        } else if (key == "--release") {
            if (!parse_double(need("--release"), &args.release)) std::exit(2);
        } else if (key == "--help" || key == "-h") {
            usage(argv[0]);
            std::exit(0);
        } else {
            std::cerr << "unknown argument: " << key << "\n";
            usage(argv[0]);
            std::exit(2);
        }
    }
    if ((args.window & (args.window - 1)) != 0) {
        std::cerr << "--window must be a power of two\n";
        std::exit(2);
    }
    args.attack = std::clamp(args.attack, 0.0, 0.99);
    args.release = std::clamp(args.release, 0.0, 0.99);
    return args;
}

std::vector<float> read_stdin_f32() {
    std::vector<float> out;
    constexpr size_t kChunkFloats = 16384;
    float buf[kChunkFloats];
    while (true) {
        size_t n = std::fread(buf, sizeof(float), kChunkFloats, stdin);
        if (n > 0) out.insert(out.end(), buf, buf + n);
        if (n < kChunkFloats) {
            if (std::ferror(stdin)) {
                std::cerr << "stdin read failed: " << std::strerror(errno) << "\n";
                std::exit(1);
            }
            break;
        }
    }
    return out;
}

double percentile(std::vector<double> values, double p) {
    if (values.empty()) return 0.0;
    std::sort(values.begin(), values.end());
    const double pos = std::clamp(p, 0.0, 100.0) / 100.0 * (values.size() - 1);
    const size_t lo = static_cast<size_t>(std::floor(pos));
    const size_t hi = std::min(values.size() - 1, lo + 1);
    const double frac = pos - lo;
    return values[lo] * (1.0 - frac) + values[hi] * frac;
}

std::vector<std::vector<double>> analyze(const std::vector<float>& samples, const Args& args) {
    std::vector<float> pcm = samples;
    if (pcm.size() < static_cast<size_t>(args.window)) pcm.resize(args.window, 0.0f);
    const size_t frame_count = 1 + (pcm.size() - args.window) / args.hop;

    std::vector<double> hann(args.window);
    for (int i = 0; i < args.window; ++i) {
        hann[i] = 0.5 - 0.5 * std::cos(2.0 * kPi * i / (args.window - 1));
    }

    std::vector<int> band_lo(9), band_hi(9);
    const double bin_hz = static_cast<double>(args.sample_rate) / args.window;
    const int max_bin = args.window / 2;
    for (int b = 0; b < 9; ++b) {
        band_lo[b] = std::max(1, static_cast<int>(std::ceil(kBands[b].low_hz / bin_hz)));
        band_hi[b] = std::min(max_bin, static_cast<int>(std::floor(kBands[b].high_hz / bin_hz)));
        if (band_hi[b] < band_lo[b]) band_hi[b] = band_lo[b];
    }

    std::vector<kiss_fft_cpx> fft_in(args.window);
    std::vector<kiss_fft_cpx> fft_out(args.window);
    kiss_fft_cfg fft_cfg = kiss_fft_alloc(args.window, 0, nullptr, nullptr);
    if (!fft_cfg) {
        std::cerr << "kiss_fft_alloc failed\n";
        std::exit(1);
    }

    std::vector<std::vector<double>> raw(frame_count, std::vector<double>(9, 0.0));
    std::vector<std::vector<double>> by_band(9);
    for (int b = 0; b < 9; ++b) by_band[b].reserve(frame_count);

    for (size_t f = 0; f < frame_count; ++f) {
        const size_t offset = f * args.hop;
        for (int i = 0; i < args.window; ++i) {
            float s = pcm[offset + i];
            if (!std::isfinite(s)) s = 0.0f;
            fft_in[i].r = static_cast<kiss_fft_scalar>(static_cast<double>(s) * hann[i]);
            fft_in[i].i = 0;
        }
        kiss_fft(fft_cfg, fft_in.data(), fft_out.data());
        for (int b = 0; b < 9; ++b) {
            double power = 0.0;
            for (int k = band_lo[b]; k <= band_hi[b]; ++k) {
                const double r = static_cast<double>(fft_out[k].r);
                const double im = static_cast<double>(fft_out[k].i);
                power += r * r + im * im;
            }
            // Average by bin count so wider high bands don't win just because
            // they contain more bins; per-band calibration handles music tilt.
            const int count = std::max(1, band_hi[b] - band_lo[b] + 1);
            power /= count;
            const double db = 10.0 * std::log10(power + kEpsilon);
            raw[f][b] = db;
            by_band[b].push_back(db);
        }
    }
    std::free(fft_cfg);

    double floor_db[9];
    double ceil_db[9];
    for (int b = 0; b < 9; ++b) {
        floor_db[b] = percentile(by_band[b], 20.0);
        ceil_db[b] = percentile(by_band[b], 97.0);
        if (!std::isfinite(floor_db[b]) || !std::isfinite(ceil_db[b]) || ceil_db[b] - floor_db[b] < 18.0) {
            const double mid = std::isfinite(ceil_db[b]) ? ceil_db[b] : -48.0;
            floor_db[b] = mid - 24.0;
            ceil_db[b] = mid;
        }
    }

    std::vector<double> env(9, 0.0);
    std::vector<std::vector<double>> out(frame_count, std::vector<double>(9, 0.0));
    for (size_t f = 0; f < frame_count; ++f) {
        for (int b = 0; b < 9; ++b) {
            double x = (raw[f][b] - floor_db[b]) / std::max(1e-9, ceil_db[b] - floor_db[b]);
            x = std::clamp(x, 0.0, 1.0);
            x = std::clamp(x * kBandGain[b], 0.0, 1.0);
            const double coef = (x > env[b]) ? args.attack : args.release;
            env[b] += (x - env[b]) * coef;
            out[f][b] = std::clamp(env[b], 0.0, 1.0);
        }
    }
    return out;
}

void write_json(const std::vector<std::vector<double>>& frames, const Args& args) {
    std::cout.setf(std::ios::fixed);
    std::cout.precision(3);
    std::cout << "{\"sample_rate\":" << args.sample_rate << ",\"hop_seconds\":";
    std::cout.precision(8);
    std::cout << (static_cast<double>(args.hop) / args.sample_rate);
    std::cout.precision(3);
    std::cout << ",\"block_size\":" << args.window
              << ",\"mode\":\"" << kMode << "\""
              << ",\"bands\":[[40,80],[80,160],[160,320],[320,640],[640,1250],[1250,2500],[2500,5000],[5000,10000],[10000,16000]]"
              << ",\"band_gain\":[0.92,0.94,0.98,1.00,1.00,1.04,1.08,1.12,1.16]"
              << ",\"frames\":[";
    for (size_t i = 0; i < frames.size(); ++i) {
        if (i) std::cout << ',';
        std::cout << '[';
        for (size_t b = 0; b < frames[i].size(); ++b) {
            if (b) std::cout << ',';
            std::cout << frames[i][b];
        }
        std::cout << ']';
    }
    std::cout << "]}\n";
}

}  // namespace

int main(int argc, char** argv) {
    Args args = parse_args(argc, argv);
    std::vector<float> samples = read_stdin_f32();
    std::vector<std::vector<double>> frames = analyze(samples, args);
    write_json(frames, args);
    return 0;
}
