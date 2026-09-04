export const recordKey = (measurementId, fieldName) => `${measurementId}::${fieldName}`;

export const unwrapPayload = (message) => {
  if (!message || typeof message !== 'object') return {};
  if (message.data && typeof message.data === 'object') return message.data;
  return message;
};

export const subjectOptions = (subjects = []) => subjects
  .map((subject) => {
    if (typeof subject === 'string') return { value: subject, label: subject };
    const value = subject?.subject_id || subject?.id;
    if (!value) return null;
    return {
      value,
      label: subject.status ? `${value} · ${subject.status}` : value
    };
  })
  .filter(Boolean);

const fieldSide = (fieldName) => {
  if (fieldName.startsWith('left_')) return '左侧';
  if (fieldName.startsWith('right_')) return '右侧';
  return '';
};

export const expandMeasurements = (definitions = []) => definitions.flatMap((definition) => {
  const fields = definition.field_names?.length ? definition.field_names : [definition.measurement_id];
  return fields.map((fieldName) => ({
    ...definition,
    field_name: fieldName,
    field_label: fieldSide(fieldName) || definition.display_name_zh
  }));
});

export const recordsToDraft = (records = []) => {
  if (!records) return {};
  if (Array.isArray(records)) {
    return Object.fromEntries(records.map((record) => [
      recordKey(record.measurement_id, record.field_name || record.measurement_id),
      {
        m1: record.m1 ?? record.values?.[0] ?? '',
        m2: record.m2 ?? record.values?.[1] ?? '',
        m3: record.m3 ?? record.values?.[2] ?? ''
      }
    ]));
  }
  return Object.fromEntries(Object.entries(records).map(([key, value]) => [
    key.includes('::') ? key : recordKey(value?.measurement_id || key, value?.field_name || key),
    {
      m1: value?.m1 ?? value?.values?.[0] ?? '',
      m2: value?.m2 ?? value?.values?.[1] ?? '',
      m3: value?.m3 ?? value?.values?.[2] ?? ''
    }
  ]));
};

const parsePositive = (value) => {
  if (value === '' || value === null || value === undefined) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : Number.NaN;
};

export const needsThirdReading = (definition, values = {}) => {
  const threshold = definition?.third_measurement_threshold;
  const first = parsePositive(values.m1);
  const second = parsePositive(values.m2);
  if (threshold === null || threshold === undefined) return false;
  if (!Number.isFinite(first) || !Number.isFinite(second)) return false;
  return Math.abs(first - second) > Number(threshold);
};

export const reduceMeasurementReadings = (definition, values = {}) => {
  const first = parsePositive(values.m1);
  const second = parsePositive(values.m2);
  const third = parsePositive(values.m3);
  if (!Number.isFinite(first) || !Number.isFinite(second)) {
    throw new Error('请填写两次大于 0 的有效读数');
  }
  if (values.m3 !== '' && values.m3 != null && !Number.isFinite(third)) {
    throw new Error('第三次读数必须大于 0');
  }

  const thresholdValue = definition?.third_measurement_threshold;
  const threshold = thresholdValue == null ? null : Number(thresholdValue);
  if (threshold != null && (!Number.isFinite(threshold) || threshold <= 0)) {
    throw new Error('复测阈值必须大于 0');
  }

  const readings = Number.isFinite(third) ? [first, second, third] : [first, second];
  const firstTwoDifference = Math.abs(first - second);
  const thirdRequired = threshold != null && firstTwoDifference > threshold;
  if (readings.length === 2) {
    if (thirdRequired) throw new Error('前两次差值超阈值，必须录入第三次测量');
    return {
      final_value: (first + second) / 2,
      selected_trial_indices: [1, 2],
      selected_difference: firstTwoDifference,
      closest_pair_difference: firstTwoDifference,
      first_two_difference: firstTwoDifference,
      third_measurement_required: false,
      reduction_rule: 'MEAN_FIRST_TWO',
      qc_status: 'PASS_2'
    };
  }

  const pairs = [[0, 1], [0, 2], [1, 2]];
  const pairDifferences = pairs.map(([left, right]) => Math.abs(readings[left] - readings[right]));
  const closestDifference = Math.min(...pairDifferences);
  const closestPairs = pairs.filter((_, index) => (
    Math.abs(pairDifferences[index] - closestDifference) <= 1e-9
  ));
  const selected = closestPairs.length === 1 ? closestPairs[0] : [0, 1, 2];
  const selectedValues = selected.map((index) => readings[index]);
  return {
    final_value: selectedValues.reduce((sum, value) => sum + value, 0) / selectedValues.length,
    selected_trial_indices: selected.map((index) => index + 1),
    selected_difference: Math.max(...selectedValues) - Math.min(...selectedValues),
    closest_pair_difference: closestDifference,
    first_two_difference: firstTwoDifference,
    third_measurement_required: thirdRequired,
    reduction_rule: closestPairs.length === 1 ? 'MEAN_CLOSEST_PAIR' : 'MEAN_ALL_THREE_TIE',
    qc_status: threshold == null || closestDifference <= threshold ? 'PASS_3' : 'REVIEW_REQUIRED'
  };
};

export const validateMeasurements = (definitions = [], draft = {}) => {
  const errors = {};
  const records = [];

  definitions.forEach((definition) => {
    const rows = expandMeasurements([definition]);
    const itemHasValue = rows.some((row) => {
      const values = draft[recordKey(row.measurement_id, row.field_name)] || {};
      return [values.m1, values.m2, values.m3].some((value) => value !== '' && value != null);
    });
    if (!definition.required && !itemHasValue) return;

    rows.forEach((row) => {
      const key = recordKey(row.measurement_id, row.field_name);
      const values = draft[key] || {};
      const first = parsePositive(values.m1);
      const second = parsePositive(values.m2);
      const third = parsePositive(values.m3);
      const rowErrors = [];
      if (!Number.isFinite(first) || !Number.isFinite(second)) {
        rowErrors.push('请填写两次大于 0 的有效读数');
      }
      if (values.m3 !== '' && values.m3 != null && !Number.isFinite(third)) {
        rowErrors.push('第三次读数必须大于 0');
      }
      if (needsThirdReading(row, values) && !Number.isFinite(third)) {
        rowErrors.push(`前两次差值超过 ${row.third_measurement_threshold}${row.unit}，必须填写第三次`);
      }
      let reduction = null;
      if (!rowErrors.length) {
        try {
          reduction = reduceMeasurementReadings(row, values);
        } catch (error) {
          rowErrors.push(error instanceof Error ? error.message : '无法归约测量读数');
        }
      }
      if (rowErrors.length) {
        errors[key] = rowErrors.join('；');
        return;
      }
      const record = {
        measurement_id: row.measurement_id,
        field_name: row.field_name,
        m1: first,
        m2: second,
        ...reduction
      };
      if (Number.isFinite(third)) record.m3 = third;
      records.push(record);
    });
  });

  return { valid: Object.keys(errors).length === 0, errors, records };
};

export const inferCameraCode = (cameraStatus = {}) => {
  const device = cameraStatus.device || {};
  if (cameraStatus.camera_code || device.camera_code) {
    return String(cameraStatus.camera_code || device.camera_code).toUpperCase();
  }
  const name = `${device.name || ''} ${device.model || ''}`.toLowerCase();
  if (name.includes('d435')) return 'CD435I';
  if (name.includes('gemini') || name.includes('336')) return 'C336L';
  return null;
};

export const resolveSelectedCondition = (
  conditions = [],
  selectedConditionId = '',
  nextConditionId = ''
) => conditions.find((condition) => condition.condition_id === selectedConditionId)
  || conditions.find((condition) => condition.condition_id === nextConditionId)
  || conditions[0]
  || null;

export const conditionLabel = (condition) => {
  if (!condition) return '尚未分配条件';
  const camera = condition.camera_code === 'CD435I' ? 'D435i' : 'Gemini 336L';
  const distance = condition.distance_mm ? `${(condition.distance_mm / 1000).toFixed(1)}m` : '--';
  const yaw = condition.view_yaw_deg ?? condition.view ?? '--';
  return `${camera} · ${distance} · ${yaw}° · ${condition.light_id || 'LSTD'} · ${condition.pose_id || 'P1'}`;
};

const asArray = (value) => {
  if (Array.isArray(value)) return value;
  if (value === null || value === undefined || value === '') return [];
  if (typeof value === 'object') {
    return Object.entries(value).map(([code, detail]) => (
      typeof detail === 'object' ? { code, ...detail } : { code, value: detail }
    ));
  }
  return [value];
};

export const isReviewRequiredStatus = (status) => [
  'REVIEW_REQUIRED', 'PENDING_REVIEW', 'AWAITING_REVIEW', 'WARN'
].includes(String(status || '').toUpperCase());

export const getReviewContext = (condition = {}, captureResult = null) => {
  const resultMatches = Boolean(
    captureResult
    && captureResult.condition_id
    && captureResult.condition_id === condition.condition_id
  );
  const result = resultMatches ? captureResult : {};
  const reconciliationRequired = Boolean(
    resultMatches && result.reconciliation_required === true
  );
  const review = condition.pending_review || condition.review || {};
  const qc = review.qc || condition.latest_qc || condition.qc || result.qc || {};
  const reviewStatus = String(review.review_status || review.status || condition.review_status || '').toUpperCase();
  const reviewDecision = String(review.decision || condition.review_decision || '').toUpperCase();
  const reviewResolved = ['ACCEPTED', 'REJECTED'].includes(reviewStatus)
    || ['ACCEPT', 'REJECT'].includes(reviewDecision);
  const resultRequestsReview = resultMatches && (
    isReviewRequiredStatus(result.quality_status)
    || result.manual_review_required === true
    || result.qc?.manual_review_required === true
  );
  const required = !reconciliationRequired && (isReviewRequiredStatus(condition.status) || (!reviewResolved && (
    review.required === true
    || condition.manual_review_required === true
    || qc.manual_review_required === true
    || resultRequestsReview
  )));
  const attempts = Array.isArray(condition.attempt_ids) ? condition.attempt_ids : [];
  const attemptId = review.attempt_id
    || condition.review_attempt_id
    || condition.pending_review_attempt_id
    || condition.latest_attempt_id
    || (resultMatches ? result.attempt_id : null)
    || (required ? attempts.at(-1) : null);
  return {
    required,
    reconciliationRequired,
    attemptId: attemptId || null,
    reasonCodes: asArray(review.reason_codes || condition.reason_codes || qc.reason_codes || qc.warning_codes),
    checks: asArray(review.checks || condition.checks || qc.checks),
    warnings: asArray(review.warnings || condition.warnings || qc.warnings),
    policyVersion: qc.policy_version || review.policy_version || result.qc?.policy_version || null
  };
};

export const createRetakeMetadata = (targetAttemptId, invalidatePrior, reason) => {
  const normalizedReason = String(reason || '').trim();
  if (!targetAttemptId) throw new Error('缺少已接受 attempt_id，不能重采');
  if (typeof invalidatePrior !== 'boolean') throw new Error('请选择旧数据处理方式');
  if (normalizedReason.length < 5) throw new Error('重采原因至少填写 5 个字');
  return {
    target_attempt_id: targetAttemptId,
    invalidate_prior: invalidatePrior,
    retake_reason: normalizedReason
  };
};
