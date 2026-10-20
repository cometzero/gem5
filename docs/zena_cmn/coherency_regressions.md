# CHI I/D 정합성 회귀 검증 (2026-10-18)

## 수정 내용

`tests/zena_cmn/directed/ruby_random.py`의 mesh router 시작 번호를
`rows × columns`로 계산한다. 기존 상수 16은 public 후보의 6×4 mesh에서
local router 검사에 실패했다. CLI에서 지정하지 않은 cache policy, prefetch,
DMT/DCT, SLC mode, 채널 수는 YAML 값을 유지한다. `--no-dmt`와
`--no-dct`는 명시적 해제이며, `--request-tbes`는 작은 요청 자원 회귀용이다.
수정은 directed fixture에 한정되며 production 설정 및 SLICC는 변경하지 않았다.

기존 `--mixed-id`는 D-side RubyTester의 데이터 검사와 별도의 I-side traffic을
결합한다. I-side PyTrafficGen 자체는 반환 payload를 검사하지 않는다.
따라서 이 결과를 I-side byte 정합성 검증으로 부르지 않으며 결과 JSON에도
`mixed_id_payload_checked: false`를 기록한다.

## 새 I-side payload 검사

`id_bytecheck.py`는 native TraceGen의 INST_FETCH 요청을 실제 L1I sequencer로
보내고, D writer/reader 및 I reader를 동일한 native MemChecker에 연결한다.
서로 다른 writer requestor ID의 byte pattern을 사용한다. 8개 주소에서 각각
초기 write → D read → I read → congruent D eviction 및 remote write → I read를
수행한다. 총 I read는 16개, 1,024 B이며 모든 generator의 완료 요청 수도
검사한다. private cache는 1 KiB/2-way, request TBE는 2로 제한한다.
이는 합성 coherence 경로 검사이며 ISA 실행, instruction synchronization,
TLBI, atomic ordering 또는 실제 A720AE 성능 검증은 아니다.

Negative control은 coherence를 고장내는 대신 마지막 remote writer만 checker
관측에서 제외한다. 다음 I read의 실제 새 값이 checker의 이전 값과 달라야
한다. 실제 `system.monitors2`가 address 0x10000에서 expected 0x5f,
received 0x64를 탐지했고 tick 5,037,001에 MemChecker violation으로 종료했다.
이는 의도된 음성 대조이며 모델 버그가 아니다. 성공 JSON이 존재하지 않고
I-side monitor와 서로 다른 byte 값이 확인돼야 oracle PASS다.

## 실행 증거

Workspace 기준 `build/gem5-zena/improvements-20261018/` 아래 artifact를 사용한다.

| artifact suffix (`coherence-`) | 조건 | 결과 |
|---|---|---|
| default | default YAML, random 1,000 | PASS, 16 mesh routers, inclusive/off |
| public | public YAML, random 1,000 | PASS, 24 mesh routers, exclusive_data/stride |
| id-positive | public, 고정 I/D schedule, DMT/DCT off | PASS, I read 16개 byte 검사 |
| id-default | default inclusive, 동일 schedule | PASS |
| id-dmt-dct | public, DMT+DCT on, 동일 schedule | PASS |
| id-negative | public, checker 미관측 remote write | expected mismatch 탐지 |
| small-mixed | public, tiny caches/TBE2, mixed random 1,000 | PASS |
| small-dmt | 같은 조건, DMT-only | PASS, Evict_Stale 526 / WriteBackFull_Stale 387 |
| small-dct | 같은 조건, DCT-only | PASS, Evict_Stale 576 / WriteBackFull_Stale 467 |

`small-mixed` L2 완료 transaction: Evict 3,893, Evict_Stale 534,
WriteBackFull 687, WriteBackFull_Stale 424. 실제 stale transaction이 관측된
random 회귀지만 정확한 race 순서를 고정한 증거는 아니다.

고정 `id-positive/protocol.log`에서 SnpOnce 126, SendSnpOnce 126,
CBWrData_SD_PD 24가 관측됐다. Local_Eviction/SendEvict에는 resource stall
재시도가 포함되므로 줄 수를 고유 transaction 수로 해석하지 않는다.
**고정 schedule에서 CBWrData_SC 및 Evict_Stale는 관측되지 않았다.** 따라서
`CHI-cache-actions.sm`의 transient clean-data 보존 조건문 전체를 통과했다거나
`CHI-cache-transitions.sm`의 Evict_Stale 처리 race를 고정 재현했다고 주장하지
않는다. 이 두 branch의 정확한 순서 재현은 남은 coverage gap이다.
Random PASS와 고정 bytecheck PASS를 합쳐 그 gap이 해소됐다고 간주하지 않는다.

## 재현

Workspace root에서 새 출력 디렉터리를 지정한다. 기존 결과를 덮어쓰지 않는다.

```sh
BASE=build/gem5-zena/coherence-new
GEM5=gem5/build/ARM/gem5.opt
PROFILE=gem5/configs/zena_cmn/profiles/a720ae_public_candidate.yaml
TEST=gem5/tests/zena_cmn/directed/id_bytecheck.py
mkdir -p "$BASE"
"$GEM5" --debug-flags=ProtocolTrace --debug-file=protocol.log \
  --outdir="$BASE/positive" "$TEST" --profile "$PROFILE" \
  >"$BASE/positive.log" 2>&1
# 의도된 negative 실행은 nonzero여야 한다. 다른 오류는 아래 oracle이 거부한다.
if "$GEM5" --outdir="$BASE/negative" "$TEST" --profile "$PROFILE" \
    --negative-control >"$BASE/negative.log" 2>&1; then
  echo 'ERROR: negative control unexpectedly succeeded'; exit 1
fi
python3 gem5/tests/zena_cmn/directed/check_id_bytecheck.py \
  --positive "$BASE/positive" --negative "$BASE/negative" \
  --negative-log "$BASE/negative.log"
"$GEM5" --outdir="$BASE/random" \
  gem5/tests/zena_cmn/directed/ruby_random.py --profile "$PROFILE" \
  --tiny-caches --request-tbes 2 --mixed-id --require-mixed-coverage --checks 1000
```

DMT-only는 `--dmt --no-dct`, DCT-only는 `--no-dmt --dct`, 둘 다 활성화는
`--dmt --dct`를 덧붙인다. Binary는 CHI 프로토콜 및 protobuf TraceGen을
포함해야 한다. 기존 multi-protocol ARM binary를 재사용했으며 재빌드는 하지 않았다.
