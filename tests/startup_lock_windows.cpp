// Regression checks for the Windows shared-memory initialization mutex.
// MinGW: g++ -std=c++17 -O2 -pthread tests/startup_lock_windows.cpp -o startup-lock-windows.exe

#include <array>
#include <chrono>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

#include "../src/shm.h"

#if !defined(_WIN32)
    #error "This test must run on Windows"
#endif

namespace {

using namespace std::chrono_literals;
using Value = std::array<Stockfish::u64, 16>;

void require(bool condition, const char* message) {
    if (!condition)
        throw std::runtime_error(message);
}

struct NamedMutex {
    HANDLE handle;
    bool   owned;

    NamedMutex(const std::string& name, bool initiallyOwned) :
        handle(CreateMutexA(nullptr, initiallyOwned, (name + "$mutex").c_str())),
        owned(initiallyOwned) {
        require(handle != nullptr, "CreateMutex failed");
    }

    ~NamedMutex() {
        if (owned)
            ReleaseMutex(handle);
        CloseHandle(handle);
    }

    void release() {
        require(ReleaseMutex(handle) != 0, "ReleaseMutex failed");
        owned = false;
    }
};

struct Result {
    bool        valid;
    bool        correctValue;
    std::string error;
};

Result initialize(const std::string& name) {
    const Value value = {1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987, 1597};
    Stockfish::SharedMemoryBackend<Value> backend(name, value);
    const bool valid = backend.is_valid();
    return {valid, valid && *static_cast<const Value*>(backend.get()) == value,
            backend.get_error_message().value_or("")};
}

void check_normal(const std::string& name) {
    auto result = initialize(name);
    require(result.valid && result.correctValue, "Uncontended initialization failed");
    std::cout << "PASS uncontended initialization\n";
}

void check_timeout(const std::string& name) {
    NamedMutex mutex(name, true);
    auto future = std::async(std::launch::async, [&]() { return initialize(name); });
    if (future.wait_for(3s) != std::future_status::ready)
    {
        // Also terminate cleanly on the original implementation's infinite wait.
        mutex.release();
        future.get();
        throw std::runtime_error("Initialization waited indefinitely on a held mutex");
    }
    auto result = future.get();
    mutex.release();
    require(!result.valid, "Initialization unexpectedly acquired a held mutex");
    require(result.error.find("Timed out") != std::string::npos,
            "Timeout was not reported accurately");
    std::cout << "PASS held mutex timeout\n";
}

void check_short_contention(const std::string& name) {
    NamedMutex mutex(name, true);
    auto future = std::async(std::launch::async, [&]() { return initialize(name); });
    std::this_thread::sleep_for(50ms);
    mutex.release();
    auto result = future.get();
    require(result.valid && result.correctValue, "Brief contention did not recover");
    std::cout << "PASS brief contention\n";
}

void check_abandoned(const std::string& name) {
    NamedMutex mutex(name, false);
    bool       acquired = false;
    std::thread owner([&]() {
        acquired = WaitForSingleObject(mutex.handle, 1000) == WAIT_OBJECT_0;
        // Deliberately exit owning the mutex to simulate a crashed initializer.
    });
    owner.join();
    require(acquired, "Could not acquire the mutex for the abandonment test");

    auto result = initialize(name);
    require(!result.valid, "Abandoned initialization should use the safe local fallback");
    require(result.error.find("abandoned") != std::string::npos,
            "Abandoned initialization was not reported accurately");

    // WAIT_ABANDONED grants ownership. Verify initialization released that ownership.
    auto future = std::async(std::launch::async, [&]() {
        DWORD status = WaitForSingleObject(mutex.handle, 1000);
        if (status == WAIT_OBJECT_0 || status == WAIT_ABANDONED)
            ReleaseMutex(mutex.handle);
        return status;
    });
    require(future.get() == WAIT_OBJECT_0, "Abandoned mutex ownership was not released");
    std::cout << "PASS abandoned mutex cleanup\n";
}

}  // namespace

int main() {
    try
    {
        const std::string prefix = "Local\\sf_startup_lock_test_"
                                 + std::to_string(GetCurrentProcessId()) + "_"
                                 + std::to_string(GetTickCount64());
        check_normal(prefix + "_normal");
        check_timeout(prefix + "_timeout");
        check_short_contention(prefix + "_brief");
        check_abandoned(prefix + "_abandoned");
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << "FAIL " << error.what() << '\n';
        return 1;
    }
}
