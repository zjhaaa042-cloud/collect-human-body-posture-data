import React from 'react';
import { Tag } from 'antd';
import { conditionLabel } from '../protocol/protocolUtils.mjs';
import { conditionStatus } from '../protocol/conditionStatus.jsx';

export default function ConditionList({ conditions, expected, selectedId, onSelect }) {
  return (
    <ol className="condition-list" aria-label={`全部 ${expected} 个采集条件`}>
      {conditions.map((condition, index) => {
        const status = conditionStatus(condition.status);
        return (
          <li key={condition.condition_id}>
            <button
              type="button"
              className={`condition-list-item ${condition.condition_id === selectedId ? 'selected' : ''}`}
              aria-current={condition.condition_id === selectedId ? 'true' : undefined}
              onClick={() => onSelect(condition.condition_id)}
            >
              <span className="condition-index">{String(index + 1).padStart(2, '0')}</span>
              <span className="condition-list-copy"><strong>{condition.condition_id}</strong><small>{conditionLabel(condition)}</small></span>
              <Tag color={status.color} icon={status.icon}>{status.text}</Tag>
            </button>
          </li>
        );
      })}
    </ol>
  );
}
