import React from 'react';
import { CheckCircleOutlined, WarningOutlined } from '@ant-design/icons';
import { Alert, Button, List, Tag, Typography } from 'antd';
import { conditionLabel } from '../protocol/protocolUtils.mjs';
import { isReviewRequiredStatus } from '../protocol/protocolUtils.mjs';

const { Text } = Typography;

export default function ReviewQueue({ conditions = [], selectedId, onSelect }) {
  const pending = conditions.filter((condition) => isReviewRequiredStatus(condition.status));
  if (!pending.length) return null;

  return (
    <section className="review-queue" aria-labelledby="review-queue-heading">
      <Alert
        type="warning"
        showIcon
        icon={<WarningOutlined />}
        message={<span id="review-queue-heading">待人工复核 {pending.length} 项</span>}
        description="采集文件已安全落盘。可先完成当前连续采集，再在此集中查看每项已保存的 F03 证据。"
      />
      <List
        size="small"
        dataSource={pending}
        renderItem={(condition) => (
          <List.Item
            actions={[<Button key="review" size="small" type={selectedId === condition.condition_id ? 'primary' : 'default'} onClick={() => onSelect(condition.condition_id)}>查看复核</Button>]}
          >
            <List.Item.Meta
              avatar={<WarningOutlined className="review-queue-icon" />}
              title={<><Text strong>{condition.condition_id}</Text> <Tag color="warning">待复核</Tag></>}
              description={conditionLabel(condition)}
            />
          </List.Item>
        )}
      />
      <Text type="secondary"><CheckCircleOutlined /> 复核完成后该条件才会计入正式完成进度。</Text>
    </section>
  );
}
