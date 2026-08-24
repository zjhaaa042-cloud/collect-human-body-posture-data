import React, { useEffect } from 'react';
import { ReloadOutlined, UserAddOutlined } from '@ant-design/icons';
import { Alert, Button, Checkbox, Divider, Form, Input, Select, Space, Typography } from 'antd';
import { subjectOptions } from '../protocol/protocolUtils.mjs';

const { Text, Title } = Typography;

const AGE_OPTIONS = ['18–24', '25–34', '35–44', '45–54', '55–64', '65+']
  .map((value) => ({ value, label: value }));
const SEX_OPTIONS = [
  { value: 'female', label: '女' },
  { value: 'male', label: '男' },
  { value: 'other', label: '其他' },
  { value: 'undisclosed', label: '不透露' }
];

export default function SubjectSetup({
  profiles,
  defaultProfileId,
  subjects,
  activeSubjectId,
  busyAction,
  onCreate,
  onSelect,
  onRefresh
}) {
  const [form] = Form.useForm();
  const profileId = Form.useWatch('profile_id', form);
  const selectedProfile = profiles.find((profile) => profile.profile_id === profileId);

  useEffect(() => {
    if (defaultProfileId && !form.getFieldValue('profile_id')) {
      form.setFieldValue('profile_id', defaultProfileId);
    }
  }, [defaultProfileId, form]);

  const createSubject = (values) => {
    onCreate({
      subject_id: values.subject_id.trim().toUpperCase(),
      profile_id: values.profile_id,
      metadata: {
        operator_id: values.operator_id.trim(),
        age_band: values.age_band || '',
        sex_category: values.sex_category || '',
        clothing_size: values.clothing_size?.trim() || '',
        consent_internal: values.consent_internal === true
      }
    });
  };

  return (
    <section aria-labelledby="subject-heading">
      <Title level={3} id="subject-heading">1. 受试者登记</Title>
      <Text type="secondary">受试者编号只使用匿名编号；当前阶段仅记录内部采集同意。</Text>
      <Form form={form} layout="vertical" onFinish={createSubject} requiredMark="optional" className="subject-form">
        <div className="form-grid">
          <Form.Item
            name="subject_id"
            label="受试者编号"
            rules={[
              { required: true, message: '请输入受试者编号' },
              { pattern: /^S\d{4}$/i, message: '格式必须为 S0001–S9999' }
            ]}
          >
            <Input placeholder="S0001" autoComplete="off" maxLength={5} />
          </Form.Item>
          <Form.Item name="profile_id" label="采集协议" rules={[{ required: true, message: '请选择采集协议' }]}>
            <Select
              placeholder="选择协议"
              options={profiles.map((profile) => ({
                value: profile.profile_id,
                label: `${profile.name} · ${profile.condition_count} 条件${profile.available === false ? ' · 当前不可用' : ''}`,
                disabled: profile.available === false
              }))}
            />
          </Form.Item>
          <Form.Item name="operator_id" label="操作员编号" rules={[{ required: true, message: '请输入操作员编号' }]}>
            <Input placeholder="OP01" autoComplete="off" />
          </Form.Item>
          <Form.Item name="age_band" label="年龄段（可选）">
            <Select allowClear placeholder="未记录" options={AGE_OPTIONS} />
          </Form.Item>
          <Form.Item name="sex_category" label="性别类别（可选）">
            <Select allowClear placeholder="未记录" options={SEX_OPTIONS} />
          </Form.Item>
          <Form.Item name="clothing_size" label="服装尺码（可选）">
            <Input placeholder="例如 L" autoComplete="off" />
          </Form.Item>
        </div>
        {selectedProfile?.requires_lux && (
          <Alert type="warning" showIcon message="此协议包含定量光照条件，需要照度计；当前设备条件下建议选择 Full-31 无照度计方案。" />
        )}
        <Form.Item
          name="consent_internal"
          valuePropName="checked"
          rules={[{
            validator: (_, checked) => checked
              ? Promise.resolve()
              : Promise.reject(new Error('请确认已取得受试者内部采集同意'))
          }]}
        >
          <Checkbox>我已核对并取得受试者对本项目内部数据采集的明确同意</Checkbox>
        </Form.Item>
        <Button type="primary" htmlType="submit" icon={<UserAddOutlined />} loading={busyAction === 'create-subject'}>
          建立受试者任务
        </Button>
      </Form>

      <Divider plain>继续已有任务</Divider>
      <Space.Compact block>
        <Select
          aria-label="选择已有受试者"
          showSearch
          optionFilterProp="label"
          placeholder={subjects.length ? '选择已有受试者' : '暂无已有任务'}
          value={activeSubjectId || undefined}
          options={subjectOptions(subjects)}
          onChange={onSelect}
          loading={busyAction === 'select-subject'}
          notFoundContent="暂无受试者任务"
        />
        <Button icon={<ReloadOutlined />} onClick={onRefresh} aria-label="刷新受试者任务列表" />
      </Space.Compact>
    </section>
  );
}
