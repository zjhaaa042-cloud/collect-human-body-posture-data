import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createRetakeMetadata,
  expandMeasurements,
  getReviewContext,
  isReviewRequiredStatus,
  needsThirdReading,
  resolveSelectedCondition,
  subjectOptions,
  validateMeasurements
} from './protocolUtils.mjs';

const definitions = [
  {
    measurement_id: 'M01', field_names: ['height_cm'], display_name_zh: '身高',
    unit: 'cm', required: true, third_measurement_threshold: 0.5
  },
  {
    measurement_id: 'M02', field_names: ['weight_kg'], display_name_zh: '体重',
    unit: 'kg', required: true, third_measurement_threshold: null
  },
  {
    measurement_id: 'M16', field_names: ['left_arm_cm', 'right_arm_cm'], display_name_zh: '左右上臂围',
    unit: 'cm', required: false, third_measurement_threshold: 1
  }
];

test('双侧测量定义展开为两个可输入字段', () => {
  const rows = expandMeasurements([definitions[2]]);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows.map((row) => row.field_label), ['左侧', '右侧']);
});

test('只有配置阈值且差值超限时要求第三测', () => {
  assert.equal(needsThirdReading(definitions[0], { m1: 170, m2: 171 }), true);
  assert.equal(needsThirdReading(definitions[1], { m1: 60, m2: 80 }), false);
});

test('必填项目拒绝缺失和超阈值未复测，选填整项空时不发送', () => {
  const first = validateMeasurements(definitions, {
    'M01::height_cm': { m1: 170, m2: 171 },
    'M02::weight_kg': { m1: 60, m2: 60.1 }
  });
  assert.equal(first.valid, false);
  assert.match(first.errors['M01::height_cm'], /第三次/);

  const second = validateMeasurements(definitions, {
    'M01::height_cm': { m1: 170, m2: 171, m3: 170.2 },
    'M02::weight_kg': { m1: 60, m2: 60.1 }
  });
  assert.equal(second.valid, true);
  assert.equal(second.records.length, 2);
});

test('受试者列表同时兼容字符串与对象', () => {
  assert.deepEqual(subjectOptions(['S001', { subject_id: 'S002', status: 'IN_PROGRESS' }]), [
    { value: 'S001', label: 'S001' },
    { value: 'S002', label: 'S002 · IN_PROGRESS' }
  ]);
});

test('相机要求跟随当前选中条件而不是协议下一项', () => {
  const conditions = [
    { condition_id: 'C_GEMINI_NEXT', camera_code: 'C336L' },
    { condition_id: 'C_D435_RETAKE', camera_code: 'CD435I' }
  ];
  assert.equal(
    resolveSelectedCondition(conditions, 'C_D435_RETAKE', 'C_GEMINI_NEXT').camera_code,
    'CD435I'
  );
  assert.equal(
    resolveSelectedCondition(conditions, 'UNKNOWN', 'C_GEMINI_NEXT').camera_code,
    'C336L'
  );
});

test('待复核上下文优先使用状态字段并展示 reason codes 与 checks', () => {
  const context = getReviewContext({
    condition_id: 'C1',
    status: 'REVIEW_REQUIRED',
    review_attempt_id: 'A002',
    qc: {
      reason_codes: ['BRIGHTNESS_LOW'],
      checks: { brightness: { status: 'WARN', value: 22.4 } }
    }
  });
  assert.equal(isReviewRequiredStatus('review_required'), true);
  assert.equal(context.required, true);
  assert.equal(context.attemptId, 'A002');
  assert.deepEqual(context.reasonCodes, ['BRIGHTNESS_LOW']);
  assert.equal(context.checks[0].code, 'brightness');
});

test('服务端状态字段暂缺时使用最新 capture result 建立复核上下文', () => {
  const context = getReviewContext(
    { condition_id: 'C1', status: 'PENDING', attempt_ids: ['A001'] },
    {
      condition_id: 'C1', attempt_id: 'A001', quality_status: 'WARN',
      qc: { manual_review_required: true, warnings: ['人体区域偏小'] }
    }
  );
  assert.equal(context.required, true);
  assert.equal(context.attemptId, 'A001');
  assert.deepEqual(context.warnings, ['人体区域偏小']);
});

test('人工复核已接受或驳回后不再被历史 WARN 锁在复核态', () => {
  const context = getReviewContext({
    condition_id: 'C1',
    status: 'NEEDS_RETAKE',
    qc: { manual_review_required: true },
    review: { review_status: 'REJECTED', decision: 'REJECT' }
  });
  assert.equal(context.required, false);
});

test('已落盘但待恢复时禁止进入人工复核流程', () => {
  const context = getReviewContext(
    { condition_id: 'C1', status: 'IN_PROGRESS' },
    {
      condition_id: 'C1', attempt_id: 'A003', quality_status: 'WARN',
      reconciliation_required: true,
      qc: { manual_review_required: true }
    }
  );
  assert.equal(context.required, false);
  assert.equal(context.reconciliationRequired, true);
});

test('已通过条件重采必须绑定旧 attempt、原因和明确作废选择', () => {
  assert.deepEqual(createRetakeMetadata('A001', false, '增加一次独立复采样本'), {
    target_attempt_id: 'A001',
    invalidate_prior: false,
    retake_reason: '增加一次独立复采样本'
  });
  assert.throws(() => createRetakeMetadata('', false, '原因足够长'), /attempt_id/);
  assert.throws(() => createRetakeMetadata('A001', null, '原因足够长'), /处理方式/);
  assert.throws(() => createRetakeMetadata('A001', true, '短'), /至少/);
});
