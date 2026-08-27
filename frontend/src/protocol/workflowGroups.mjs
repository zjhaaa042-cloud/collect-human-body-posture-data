const GROUPS = {
  gemini_natural_clothing: {
    id: 'gemini-natural',
    title: 'Gemini · 日常服',
    description: '先完成入场日常服视角，随后仅需更换一次标准贴身采集服。'
  },
  gemini_view: {
    id: 'gemini-standard',
    title: 'Gemini · 标准视角',
    description: '按角度地垫连续转身；保持同一相机、距离与服装。'
  },
  gemini_distance: {
    id: 'gemini-variation',
    title: 'Gemini · 距离与姿态',
    description: '按提示调整脚位或姿态，系统会突出显示变化项。'
  },
  gemini_repositioning: {
    id: 'gemini-variation',
    title: 'Gemini · 距离与姿态',
    description: '按提示调整脚位或姿态，系统会突出显示变化项。'
  },
  gemini_pose: {
    id: 'gemini-variation',
    title: 'Gemini · 距离与姿态',
    description: '按提示调整脚位或姿态，系统会突出显示变化项。'
  },
  d435i_view: {
    id: 'd435i',
    title: 'D435i · 视角与距离',
    description: '切换设备后完成 D435i 的跨设备参考条件。'
  },
  d435i_distance: {
    id: 'd435i',
    title: 'D435i · 视角与距离',
    description: '切换设备后完成 D435i 的跨设备参考条件。'
  },
  d435i_repositioning: {
    id: 'd435i',
    title: 'D435i · 视角与距离',
    description: '切换设备后完成 D435i 的跨设备参考条件。'
  }
};

const fallbackGroup = (condition = {}) => ({
  id: `camera-${condition.camera_code || 'unknown'}`,
  title: condition.camera_code === 'CD435I' ? 'D435i 采集' : 'Gemini 采集',
  description: '按当前协议顺序完成该组条件。'
});

export const workflowGroupFor = (condition) => GROUPS[condition?.suite] || fallbackGroup(condition);

export const buildWorkflowGroups = (conditions = []) => {
  const groups = [];
  conditions.forEach((condition) => {
    const group = workflowGroupFor(condition);
    const previous = groups.at(-1);
    if (!previous || previous.id !== group.id) {
      groups.push({ ...group, conditions: [condition] });
    } else {
      previous.conditions.push(condition);
    }
  });
  return groups;
};

const CHANGE_LABELS = {
  camera_code: '相机',
  distance_mm: '距离/脚位',
  view_yaw_deg: '朝向',
  pose_id: '姿态',
  clothing_id: '服装',
  repeat_id: '重新站位'
};

export const setupChanges = (previous, current) => {
  if (!current) return [];
  if (!previous) return ['首次就位'];
  return Object.entries(CHANGE_LABELS)
    .filter(([key]) => previous[key] !== current[key])
    .map(([, label]) => label);
};
