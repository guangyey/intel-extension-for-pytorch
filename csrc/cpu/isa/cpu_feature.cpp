#include "cpu_feature.hpp"
#include <stdio.h>
#include "embedded_function.h"

#ifdef __linux__
#include <sys/syscall.h>
#include <unistd.h>
#endif

namespace torch_ipex {
namespace cpu {
CPUFeature::CPUFeature() {
  detect_intel_cpu_feature();
}

CPUFeature& CPUFeature::get_instance() {
  static CPUFeature _instance;

  return _instance;
}

void CPUFeature::detect_intel_cpu_feature() {
  uint32_t eax = 0;
  uint32_t ebx = 0;
  uint32_t ecx = 0;
  uint32_t edx = 0;

  /*
  Initial version reference from:
  ----------------------------------------------------
  Intel® Architecture
  Instruction Set Extensions
  and Future Features
  Programming Reference
  May 2021
  319433-044
  */
  read_cpuid(0, &eax, &ebx, &ecx, &edx);
  uint32_t max_basic_id = eax;

  read_cpuid(0x80000000, &eax, &ebx, &ecx, &edx);
  uint32_t max_extend_id = eax;

  if (max_basic_id >= 0x00000001) {
    read_cpuidex(0x00000001, 0, &eax, &ebx, &ecx, &edx);

    MICRO_CLASS_MEMBER(mmx) = check_reg_bit(edx, 23);
    MICRO_CLASS_MEMBER(sse) = check_reg_bit(edx, 25);
    MICRO_CLASS_MEMBER(sse2) = check_reg_bit(edx, 26);
    MICRO_CLASS_MEMBER(sse3) = check_reg_bit(ecx, 0);
    MICRO_CLASS_MEMBER(ssse3) = check_reg_bit(ecx, 9);
    MICRO_CLASS_MEMBER(sse4_1) = check_reg_bit(ecx, 19);
    MICRO_CLASS_MEMBER(sse4_2) = check_reg_bit(ecx, 20);
    MICRO_CLASS_MEMBER(aes_ni) = check_reg_bit(ecx, 25);
    MICRO_CLASS_MEMBER(xsave) = check_reg_bit(ecx, 26);

    MICRO_CLASS_MEMBER(avx) = check_reg_bit(ecx, 28);

    MICRO_CLASS_MEMBER(fma) = check_reg_bit(ecx, 12);
    MICRO_CLASS_MEMBER(f16c) = check_reg_bit(ecx, 29);
  }

  if (max_basic_id >= 0x00000007) {
    uint32_t max_sub_leaf = 0;
    read_cpuidex(0x00000007, 0, &eax, &ebx, &ecx, &edx);
    max_sub_leaf = eax;

    MICRO_CLASS_MEMBER(avx2) = check_reg_bit(ebx, 5);
    MICRO_CLASS_MEMBER(sha) = check_reg_bit(ebx, 29);

    MICRO_CLASS_MEMBER(avx512_f) = check_reg_bit(ebx, 16);
    MICRO_CLASS_MEMBER(avx512_cd) = check_reg_bit(ebx, 28);
    MICRO_CLASS_MEMBER(avx512_pf) =
        check_reg_bit(ebx, 26); // (Intel® Xeon Phi™ only.)
    MICRO_CLASS_MEMBER(avx512_er) =
        check_reg_bit(ebx, 27); // (Intel® Xeon Phi™ only.)
    MICRO_CLASS_MEMBER(avx512_vl) = check_reg_bit(ebx, 31);
    MICRO_CLASS_MEMBER(avx512_bw) = check_reg_bit(ebx, 30);
    MICRO_CLASS_MEMBER(avx512_dq) = check_reg_bit(ebx, 17);
    MICRO_CLASS_MEMBER(avx512_ifma) = check_reg_bit(ebx, 21);

    MICRO_CLASS_MEMBER(prefetchwt1) =
        check_reg_bit(ecx, 0); // (Intel® Xeon Phi™ only.)
    MICRO_CLASS_MEMBER(avx512_vbmi) = check_reg_bit(ecx, 1);
    MICRO_CLASS_MEMBER(avx512_vpopcntdq) = check_reg_bit(ecx, 14);
    MICRO_CLASS_MEMBER(avx512_vbmi2) = check_reg_bit(ecx, 6);
    MICRO_CLASS_MEMBER(avx512_vpclmul) = check_reg_bit(ecx, 10);
    MICRO_CLASS_MEMBER(avx512_vnni) = check_reg_bit(ecx, 11);
    MICRO_CLASS_MEMBER(avx512_bitalg) = check_reg_bit(ecx, 12);

    MICRO_CLASS_MEMBER(avx512_4fmaps) =
        check_reg_bit(edx, 3); // (Intel® Xeon Phi™ only.)
    MICRO_CLASS_MEMBER(avx512_4vnniw) =
        check_reg_bit(edx, 2); // (Intel® Xeon Phi™ only.)
    MICRO_CLASS_MEMBER(avx512_vp2intersect) = check_reg_bit(edx, 8);
    MICRO_CLASS_MEMBER(avx512_fp16) = check_reg_bit(edx, 23);

    MICRO_CLASS_MEMBER(amx_bf16) = check_reg_bit(edx, 22);
    MICRO_CLASS_MEMBER(amx_tile) = check_reg_bit(edx, 24);
    MICRO_CLASS_MEMBER(amx_int8) = check_reg_bit(edx, 25);

    if (max_sub_leaf >= 1) {
      read_cpuidex(0x00000007, 1, &eax, &ebx, &ecx, &edx);

      MICRO_CLASS_MEMBER(avx_vnni) = check_reg_bit(eax, 4);
      MICRO_CLASS_MEMBER(avx512_bf16) = check_reg_bit(eax, 5);
    }
  }

  if (max_extend_id >= 0x80000001) {
    read_cpuidex(0x80000001, 0, &eax, &ebx, &ecx, &edx);

    MICRO_CLASS_MEMBER(prefetchw) = check_reg_bit(ecx, 8);
  }
}

bool CPUFeature::os_avx() {
  bool support_avx = false;
  uint32_t eax = 0;
  uint32_t ebx = 0;
  uint32_t ecx = 0;
  uint32_t edx = 0;

  read_cpuid(0, &eax, &ebx, &ecx, &edx);
  uint32_t max_basic_id = eax;
  if (max_basic_id >= 0x00000001) {
    read_cpuidex(0x00000001, 0, &eax, &ebx, &ecx, &edx);

    support_avx = check_reg_bit(ecx, 28);
  }

  if (!support_avx) {
    return false;
  }

#ifdef ENABLE_XCR_CHECK
  uint64_t xcrFeatureMask = 0;
  bool ret = read_xcr(_XCR_XFEATURE_ENABLED_MASK, &xcrFeatureMask);
  if (!ret) {
    return false;
  }

  /*
    Intel® 64 and IA-32 Architectures
    Software Developer’s Manual
    Combined Volumes:
    1, 2A, 2B, 2C, 2D, 3A, 3B, 3C, 3D and 4
    Order Number: 325462-075US
    June 2021
    ----------------------------------------------------
    13.1 XSAVE-SUPPORTED FEATURES AND STATE-COMPONENT BITMAPS
    ......
    Bit 1 corresponds to the state component used for registers used by the
    streaming SIMD extensions (SSE state). See Section 13.5.2. Bit 2 corresponds
    to the state component used for the additional register state used by the
    Intel® Advanced Vector Extensions (AVX state). See Section 13.5.3
  */
  uint32_t avx_feature_bits = BIT_M_TO_N_64(xcrFeatureMask, 1, 2);
  if (avx_feature_bits != 0b11) {
    return false;
  }
#endif

  return true;
}

bool CPUFeature::os_avx2() {
  bool support_avx2 = false;
  uint32_t eax = 0;
  uint32_t ebx = 0;
  uint32_t ecx = 0;
  uint32_t edx = 0;

  read_cpuid(0, &eax, &ebx, &ecx, &edx);
  uint32_t max_basic_id = eax;
  if (max_basic_id >= 0x00000007) {
    uint32_t max_sub_leaf = 0;
    read_cpuidex(0x00000007, 0, &eax, &ebx, &ecx, &edx);

    support_avx2 = check_reg_bit(ebx, 5);
  }

  if (!support_avx2) {
    return false;
  }

#ifdef ENABLE_XCR_CHECK
  uint64_t xcrFeatureMask = 0;
  bool ret = read_xcr(_XCR_XFEATURE_ENABLED_MASK, &xcrFeatureMask);
  if (!ret) {
    return false;
  }

  /*
    Intel® 64 and IA-32 Architectures
    Software Developer’s Manual
    Combined Volumes:
    1, 2A, 2B, 2C, 2D, 3A, 3B, 3C, 3D and 4
    Order Number: 325462-075US
    June 2021
    ----------------------------------------------------
    13.1 XSAVE-SUPPORTED FEATURES AND STATE-COMPONENT BITMAPS
    ......
    Bit 1 corresponds to the state component used for registers used by the
    streaming SIMD extensions (SSE state). See Section 13.5.2. Bit 2 corresponds
    to the state component used for the additional register state used by the
    Intel® Advanced Vector Extensions (AVX state). See Section 13.5.3
  */
  uint32_t avx_feature_bits = BIT_M_TO_N_64(xcrFeatureMask, 1, 2);
  if (avx_feature_bits != 0b11) {
    return false;
  }
#endif

  return true;
}

bool CPUFeature::os_avx512() {
#ifdef ENABLE_XCR_CHECK
  uint64_t xcrFeatureMask = 0;
  bool ret = read_xcr(_XCR_XFEATURE_ENABLED_MASK, &xcrFeatureMask);
  if (!ret) {
    return false;
  }

  /*
  Intel® 64 and IA-32 Architectures
  Software Developer’s Manual
  Combined Volumes:
  1, 2A, 2B, 2C, 2D, 3A, 3B, 3C, 3D and 4
  Order Number: 325462-075US
  June 2021
  ----------------------------------------------------
  13.1 XSAVE-SUPPORTED FEATURES AND STATE-COMPONENT BITMAPS
  ......
  Bits 7:5 correspond to the three state components used for the additional
  register state used by Intel® Advanced Vector Extensions 512 (AVX-512 state):
  — State component 5 is used for the 8 64-bit opmask registers k0–k7 (opmask
  state). 13-2 Vol. 1 MANAGING STATE USING THE XSAVE FEATURE SET — State
  component 6 is used for the upper 256 bits of the registers ZMM0–ZMM15. These
  16 256-bit values are denoted ZMM0_H–ZMM15_H (ZMM_Hi256 state). — State
  component 7 is used for the 16 512-bit registers ZMM16–ZMM31 (Hi16_ZMM state).
  */
  uint32_t avx512_feature_bits = BIT_M_TO_N_64(xcrFeatureMask, 5, 7);
  if (avx512_feature_bits != 0b111) {
    return false;
  }
#endif

  return true;
}

bool CPUFeature::os_amx() {
#ifdef ENABLE_XCR_CHECK
  uint64_t xcrFeatureMask = 0;
  bool ret = read_xcr(_XCR_XFEATURE_ENABLED_MASK, &xcrFeatureMask);
  if (!ret) {
    return false;
  }

  /*
          Initial version reference from:
  ----------------------------------------------------
  Intel® Architecture
  Instruction Set Extensions
  and Future Features
  Programming Reference
  May 2021
  319433-044
  ----------------------------------------------------
  3.2.1 State Components for Intel® AMX
  As noted earlier, the XSAVE feature set supports the saving and restoring of
  state components, each of which is a discrete set of processor registers (or
  parts of registers). Each state component corresponds to a particular CPU
  feature. (Some XSAVE-supported features use registers in multiple
  XSAVE-managed state components.) The XSAVE feature set organizes state
  components using state-component bitmaps. A state-component bitmap comprises
  64 bits; each bit in such a bitmap corresponds to a single state component.
  Intel AMX defines bits 18:17 for its state components (collectively, these are
  called AMX state): • State component 17 is used for the 64-byte TILECFG
  register (XTILECFG state). • State component 18 is used for the 8192 bytes of
  tile data (XTILEDATA state).
  */
  uint32_t avx512_feature_bits = BIT_M_TO_N_64(xcrFeatureMask, 17, 18);
  if (avx512_feature_bits != 0b11) {
    return false;
  }
#endif

  return true;
}

#ifdef __linux__

#define XFEATURE_XTILECFG 17
#define XFEATURE_XTILEDATA 18
#define XFEATURE_MASK_XTILECFG (1 << XFEATURE_XTILECFG)
#define XFEATURE_MASK_XTILEDATA (1 << XFEATURE_XTILEDATA)
#define XFEATURE_MASK_XTILE (XFEATURE_MASK_XTILECFG | XFEATURE_MASK_XTILEDATA)
#define ARCH_GET_XCOMP_PERM 0x1022
#define ARCH_REQ_XCOMP_PERM 0x1023

bool CPUFeature::init_amx() {
  unsigned long bitmask = 0;
  long status = syscall(SYS_arch_prctl, ARCH_GET_XCOMP_PERM, &bitmask);
  if (0 != status)
    return false;
  if (bitmask & XFEATURE_MASK_XTILEDATA)
    return true;

  status = syscall(SYS_arch_prctl, ARCH_REQ_XCOMP_PERM, XFEATURE_XTILEDATA);
  if (0 != status)
    return false; // XFEATURE_XTILEDATA setup is failed, TMUL usage is not
                  // allowed
  status = syscall(SYS_arch_prctl, ARCH_GET_XCOMP_PERM, &bitmask);

  // XFEATURE_XTILEDATA setup is failed, can't use TMUL
  if (0 != status || !(bitmask & XFEATURE_MASK_XTILEDATA))
    return false;

  // XFEATURE_XTILEDATA set successfully, TMUL usage is allowed
  return true;
}
#else
bool CPUFeature::init_amx() {
  AT_ERROR("DispatchStub: only support init amx on Linux now");
  return false;
}
#endif

bool CPUFeature::isa_level_avx2() {
  static bool b_is_support = os_avx2() && cpuid_avx2() && cpuid_fma();
  return b_is_support;
}

bool CPUFeature::isa_level_avx2_vnni() {
  static bool b_is_support = isa_level_avx2() && cpuid_avx_vnni();
  return b_is_support;
}

bool CPUFeature::isa_level_avx512_core() {
  static bool b_is_support = isa_level_avx2() && os_avx512() &&
      cpuid_avx512_vl() && cpuid_avx512_bw() && cpuid_avx512_dq() &&
      cpuid_avx512_f();
  return b_is_support;
}

bool CPUFeature::isa_level_avx512_vnni() {
  static bool b_is_support = isa_level_avx512_core() && cpuid_avx512_vnni();
  return b_is_support;
}

bool CPUFeature::isa_level_avx512_bf16() {
  static bool b_is_support = isa_level_avx512_vnni() && cpuid_avx512_bf16();
  return b_is_support;
}

bool CPUFeature::_do_check_and_init_amx() {
  bool b_is_support = isa_level_avx512_bf16() && os_amx() && cpuid_amx_bf16() &&
      cpuid_amx_int8() && cpuid_amx_tile();
  if (b_is_support) {
    b_is_support = init_amx();
  }
  return b_is_support;
}

bool CPUFeature::isa_level_amx() {
  // check and init in a funtion, avoid to double init.
  static bool b_is_support = _do_check_and_init_amx();

  return b_is_support;
}

bool CPUFeature::isa_level_avx512_fp16() {
  // check and init in a funtion, avoid to double init.
  static bool b_is_support =
      isa_level_avx512_vnni() && isa_level_amx() && cpuid_avx512_fp16();
  return b_is_support;
}

__forceinline void print_bool_status(const char* p_name, bool b_status) {
  printf("%s:\t\t\t%s\n", p_name, (b_status ? "true" : "false"));
}

void CPUFeature::show_features() {
#ifdef CPU_FEATURE_EXEC
  MICRO_CLASS_PRINT_BOOL_STATUS(mmx);
  MICRO_CLASS_PRINT_BOOL_STATUS(sse);
  MICRO_CLASS_PRINT_BOOL_STATUS(sse2);
  MICRO_CLASS_PRINT_BOOL_STATUS(sse3);
  MICRO_CLASS_PRINT_BOOL_STATUS(ssse3);
  MICRO_CLASS_PRINT_BOOL_STATUS(sse4_1);
  MICRO_CLASS_PRINT_BOOL_STATUS(sse4_2);
  MICRO_CLASS_PRINT_BOOL_STATUS(aes_ni);
  MICRO_CLASS_PRINT_BOOL_STATUS(sha);

  MICRO_CLASS_PRINT_BOOL_STATUS(xsave);

  MICRO_CLASS_PRINT_BOOL_STATUS(fma);
  MICRO_CLASS_PRINT_BOOL_STATUS(f16c);

  MICRO_CLASS_PRINT_BOOL_STATUS(avx);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx2);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx_vnni);

  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_f);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_cd);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_pf);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_er);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_vl);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_bw);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_dq);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_ifma);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_vbmi);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_vpopcntdq);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_4fmaps);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_4vnniw);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_vbmi2);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_vpclmul);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_vnni);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_bitalg);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_fp16);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_bf16);
  MICRO_CLASS_PRINT_BOOL_STATUS(avx512_vp2intersect);

  MICRO_CLASS_PRINT_BOOL_STATUS(amx_bf16);
  MICRO_CLASS_PRINT_BOOL_STATUS(amx_tile);
  MICRO_CLASS_PRINT_BOOL_STATUS(amx_int8);

  MICRO_CLASS_PRINT_BOOL_STATUS(prefetchw);
  MICRO_CLASS_PRINT_BOOL_STATUS(prefetchwt1);
#endif
}
} // namespace cpu
} // namespace torch_ipex
