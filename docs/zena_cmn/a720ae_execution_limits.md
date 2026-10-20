# A720AE 명령 실행 특성 검증 범위

`run_roofline.py --execution-checks`는 16CPU Linux 모델의 CPU0에서
401–412번 진단을 실행한다. 이 검사는 지정한 AArch64 명령과 의존 관계의
모델 동작을 확인하며, 실물 성능 보정을 완료한 것으로 판정하지 않는다.
FU latency와 CPU 내부 구현은 변경하지 않았다.

## 산술·나눗셈 검사

각 검사는 명령 warmup 후 1024회 × 대상 명령 8개를 측정한다.
408번은 scalar divide 8개와 독립 vector add 8개를 교대로 실행한다.
Checksum 비교와 출력은 ROI 밖에 있다. 반환값 reduction, 루프·분기 및
marker 비용은 ROI에 포함하지만 알고리즘 FLOP 수에는 더하지 않는다.

| ID | 정확한 명령/의존 관계 | SOG 기준 | 측정 cycles/대상 명령 |
|---|---|---|---:|
|401|FP32 Q FMLA, 독립 accumulator 8개|2명령/cycle|0.5028|
|402|FP32 Q FMLA, accumulator 의존 chain|accumulator forwarding 2cycles|3.9426|
|403|FP32 Q FMLA, 두 register가 multiplicand를 교대로 공급|일반 latency 4cycles|3.9510|
|404|FP32 Q FMLA, destination과 multiplicand alias|별도 alias 진단, 합격 기준 미지정|3.9509|
|405|FP32 Q FDIV, 독립 destination 8개|1/4명령/cycle|0.8790|
|406|FP32 Q FDIV, numerator 의존 chain|latency 10cycles|1.0015|
|407|Scalar FP32 FDIV, 독립 destination 8개|1명령/cycle, latency 7cycles|6.1187|
|408|Scalar FP32 FDIV + 독립 FP32 Q FADD|공유 자원 진단, 합격 기준 미지정|3.3112|

408번의 분모는 divide와 add를 합친 16384개다. Scalar divide 하나당
시간은 6.6224cycles이며 407번과 비교할 때 이 분모를 사용한다.
독립 FMLA와 일반 의존 latency는 문서 수치에 가깝지만 accumulator
forwarding과 divider는 차이가 크다. 모든 행의 하드웨어 판정은
`UNQUALIFIED_HARDWARE_MODEL_DIAGNOSTIC`으로 유지한다.

출처는 Arm Cortex-A720AE Software Optimization Guide 109690 Issue 2.0의
3.12 scalar FP 표/주석 및 3.17 ASIMD FP 표/주석(인쇄 pp.31–34)이다.
로컬 출처는 `build/gem5-zena/improvements-20261018/sources/a720ae-sog.pdf`
및 `.txt`다. FMLA의 4(2) 중 괄호는 accumulator forwarding에 해당하며
모든 입력 의존성에 적용하지 않는다. Q FP64 FDIV의 13cycles·1/2명령/cycle는
다른 형식이며 이번 FP32 검사로 검증하지 않았다.

FMLA general/alias 검사는 zero multiplicand로 유한한 checksum과 register
의존성을 유지한다. Divide chain은 1/1, 독립 vector divide는 1/2를 사용한다.
Denormal·예외·operand별 early-out·FP64·rounding mode는 미검증이다.
Mixed vector add는 독립 상수 입력을 사용하므로 accumulator chain이 아니다.
Inline assembly의 early-clobber 제약으로 상수 입력과 출력의 의도하지 않은
register alias를 방지했으며 disassembly를 보존했다.

FLOP 수는 vector FMLA 8/명령, FP32 Q FDIV/FADD 4/명령,
scalar FDIV 1/명령이다. 산술 검사의 useful memory byte는 0이다.
Generic opClass는 형식별 divider 및 operand별 bypass를 모두 표현하지
못하므로 checksum PASS를 전체 FP 명령의 calibration PASS로 해석하지 않는다.

## Warm L1 LSU 검사

64B 정렬한 8KiB를 8회 warmup하고 전체 footprint를 다시 쓰며 값을 준비한 뒤
8회 측정한다. Useful traffic은 검사당 64KiB다. 반환 register 보존용 64B
store와 제어 비용은 ROI 안에 있으나 useful traffic에는 포함하지 않는다.
배열 전체와 반환 register의 값 검사는 ROI 밖에서 수행한다.
Store 검사는 warmup 이후 값을 바꾸므로 ROI store 누락을 검출한다.

| ID | 64B 단위의 대상 명령 | 대상 명령 수 | Read / Write B | Useful B/cycle | Native committed load / store |
|---|---|---:|---:|---:|---:|
|409|LDR Q ×4|4096|65536 /0|10.4174|4098 /10|
|410|LDP Q ×2|2048|65536 /0|5.2635|4098 /10|
|411|STP Q ×2|2048|0 /65536|4.8773|2 /8202|
|412|LDP Q + STP Q, 같은 line의 뒤쪽 절반으로 복사|2048|32768 /32768|7.8712|2050 /4106|

LDR Q의 committed load/대상 명령 비율은 약1, LDP Q는 약2,
STP Q의 committed store/대상 명령 비율은 약4다. Mixed는 LDP/STP 각각
1024개를 실행하여 약2/4의 비율이다. 이 native 카운터에는 ROI 내 반환값
저장·stack 등도 포함하므로 대상 명령만의 정확한 micro-op 수라고 부르지 않는다.

네 검사 모두 native L1 demand-hit 기준 `QUALIFIED_MODEL_PROXY`를 통과했다.
Footprint 크기만으로 L1 residency를 추정하지 않는다. Mixed는 명시적인
load→store data 의존성을 포함하므로 독립 LSU issue 포화 실험이 아니다.
명령별 주소 계산·분기 비용도 포함되며, useful B/cycle는 애플리케이션 byte
기준이다. Cache fabric byte/cycle나 dirty data의 DRAM write 완료가 아니다.

## 실행 증거와 metadata 정정

`build/gem5-zena/improvements-20261018/execution-linux-v2`는
302.835초에 12/12 checksum, 정확한 ROI 통계, 정상 종료를 통과했다.
`calibration-summary.json`, `m5out/config-verification.json`,
`execution-disassembly-v2.txt`를 참조한다. 먼저 수행한 8개 산술 검사는
별도 `execution-linux`에 남겼다.

v2 guest의 LSU 행에 출력된 `warmup_loads:1024`는 실제 load 수가 아니라
loop block 수였으므로 사용하지 않는다. 분석기는 warmup 8회/8KiB를
명시하고 원본 필드를 보존한다. 현재 소스는 사용하지 않는 공통 필드를 0으로
정정했다. 이 변경은 ROI 밖 출력 metadata 인자만 바꾸며, v2 실행의 source와
binary는 `guest/`에 SHA와 함께 보존했다. 정정된 binary는 static AArch64
빌드 및 QEMU user-mode에서 12개 checksum과 metadata를 재검증했다.
정정된 binary의 Linux timing 실행을 다시 수행했다는 의미는 아니다.
