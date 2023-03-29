#pragma once

#include <ATen/Context.h>
#include <ATen/core/ATenGeneral.h>
#include <runtime/Device.h>
#include <utils/DPCPP.h>

namespace xpu {
namespace dpcpp {

class CachingHostAllocator final {
 private:
  class Block {
   public:
    Block(DeviceId device, size_t size, void* ptr = nullptr)
        : mDevId(device), mSize(size), mPtr(ptr) {}

    static bool Comparator(const Block& ablock, const Block& bblock) {
      if (ablock.mDevId != bblock.mDevId) {
        return ablock.mDevId < bblock.mDevId;
      }
      if (ablock.mSize != bblock.mSize) {
        return ablock.mSize < bblock.mSize;
      }
      return (uintptr_t)ablock.mPtr < (uintptr_t)bblock.mPtr;
    }

    void* getPtr() const;
    DeviceId getDevice() const;

   private:
    DeviceId mDevId;
    size_t mSize;
    void* mPtr;
  };

  class BlockState : public Block {
   public:
    BlockState(DeviceId device, size_t size, void* ptr, bool allocated = false)
        : Block(device, size, ptr), mAllocated(allocated), mEvents() {}

    bool hasEvent();

    void insertEvent(sycl::event& e);

    void processEvents();

    bool isAllocated();

    void setAllocated(bool alloc);

   private:
    bool mAllocated;
    std::deque<sycl::event> mEvents;
  };

  CachingHostAllocator();

  ~CachingHostAllocator();

  void processEvents();

  std::mutex mMutex;
  std::unordered_map<void*, BlockState> mBlocks;
  std::set<Block, decltype(Block::Comparator)*> mAvailable;

 public:
  static CachingHostAllocator* Instance(); // Singleton

  bool isHostPtr(void* ptr);

  void emptyCache();

  void recordEvent(void* ptr, sycl::event& e);

  int malloc(void** ptr, size_t size);

  void release(void* ptr);
};

} // namespace dpcpp
} // namespace xpu