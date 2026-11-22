import { StatusBadge } from '../../../components/StatusBadge.jsx'
import { formatTime } from '../../../utils/dateTime.js'
import { friendlyError, groupValidationIssues, issueLabel, issueSeverity } from '../../../utils/labels.js'

export function TypeImportPanel({
  file,
  result,
  busy,
  onDownloadTemplate,
  onFileChange,
  onSubmit,
}) {
  return (
    <div className="type-import-panel">
      <div className="type-import-head">
        <div>
          <strong>批量修正字段类型</strong>
          <p className="muted-text">
            表格按「报表代码 / 字段英文名 / 目标类型 / 目标长度 / 目标小数位」填写，只写要改的行；导入后这些字段标为「人工确认」。
          </p>
        </div>
        <div className="actions">
          <button type="button" className="button secondary sm" onClick={onDownloadTemplate}>
            下载模板（含当前字段）
          </button>
          <label className="button secondary sm file-button">
            选择文件
            <input
              type="file"
              accept=".csv,.xlsx"
              onChange={(e) => onFileChange(e.target.files?.[0] || null)}
            />
          </label>
          <span className="muted-text">{file ? file.name : '未选择文件'}</span>
        </div>
      </div>
      <div className="actions" style={{ marginTop: 10 }}>
        <button type="button" className="button secondary sm" disabled={busy || !file} onClick={() => onSubmit(true)}>
          先预览（不改数据）
        </button>
        <button type="button" className="button primary sm" disabled={busy || !file} onClick={() => onSubmit(false)}>
          确认导入
        </button>
      </div>
      {result && (
        <div className="type-import-result">
          <div className="actions">
            <StatusBadge tone={result.updated > 0 ? 'green' : 'neutral'}>
              {result.dry_run ? '预览结果' : '已导入'} · {result.updated} 行
            </StatusBadge>
            <span className="muted-text">
              无变化 {result.unchanged} 行 · 未导入 {result.errors.length} 行
            </span>
          </div>
          {(result.details || []).length > 0 && (
            <ul className="type-import-list">
              {result.details.slice(0, 12).map((item) => (
                <li key={`${item.line}-${item.field_en}`}>
                  <code>{item.table_code}.{item.field_en}</code>
                  <span>
                    {Object.entries(item.changes)
                      .map(([key, value]) => `${key}=${value}`)
                      .join('，')}
                  </span>
                </li>
              ))}
              {result.details.length > 12 && (
                <li className="muted-text">另有 {result.details.length - 12} 行…</li>
              )}
            </ul>
          )}
          {(result.errors || []).length > 0 && (
            <ul className="type-import-list danger-text">
              {result.errors.slice(0, 12).map((item) => (
                <li key={`err-${item.line}`}>
                  第 {item.line} 行：{item.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

export function AddFieldForm({ newField, busy, onChange, onSubmit }) {
  return (
    <div className="add-field-form">
      <label>
        <span>中文名称 *</span>
        <input
          placeholder="例：申报号码"
          value={newField.field_cn}
          onChange={(e) => onChange({ ...newField, field_cn: e.target.value })}
        />
      </label>
      <label>
        <span>英文列名 *</span>
        <input
          placeholder="例：RPTNO"
          value={newField.field_en}
          onChange={(e) => onChange({ ...newField, field_en: e.target.value.toUpperCase() })}
        />
      </label>
      <label>
        <span>数据类型</span>
        <select
          value={newField.data_type}
          onChange={(e) => {
            const nextType = e.target.value
            onChange({
              ...newField,
              data_type: nextType,
              decimal_places: nextType === 'NUMBER' ? newField.decimal_places : 0,
            })
          }}
        >
          <option value="VARCHAR">文本 (VARCHAR)</option>
          <option value="NUMBER">数值 (NUMBER)</option>
        </select>
      </label>
      <label>
        <span>字段长度</span>
        <input
          type="number"
          min="1"
          value={newField.length}
          onChange={(e) => onChange({ ...newField, length: e.target.value })}
        />
      </label>
      {newField.data_type === 'NUMBER' && (
        <label>
          <span>小数位数</span>
          <input
            type="number"
            min="0"
            value={newField.decimal_places}
            onChange={(e) => onChange({ ...newField, decimal_places: e.target.value })}
          />
        </label>
      )}
      <label>
        <span>字段定义</span>
        <input
          placeholder="业务含义说明"
          value={newField.field_definition}
          onChange={(e) => onChange({ ...newField, field_definition: e.target.value })}
        />
      </label>
      <label>
        <span>原始校验规则</span>
        <input
          placeholder="例：必填，22位定长字符"
          value={newField.check_text_raw}
          onChange={(e) => onChange({ ...newField, check_text_raw: e.target.value })}
        />
      </label>
      <label>
        <span>码值表引用</span>
        <input
          placeholder="无引用可留空"
          value={newField.code_reference_text}
          onChange={(e) => onChange({ ...newField, code_reference_text: e.target.value })}
        />
      </label>
      <div className="actions" style={{ alignSelf: 'end' }}>
        <label className="check-label" style={{ paddingTop: 0 }}>
          <input
            type="checkbox"
            checked={newField.required}
            onChange={(e) => onChange({ ...newField, required: e.target.checked })}
          />
          必填
        </label>
        <button type="button" className="button primary" disabled={busy} onClick={onSubmit}>
          确认添加
        </button>
      </div>
    </div>
  )
}

export function ValidationTabView({ validations, fields, onLocateField }) {
  return (
    <div className="validation-list">
      {validations.length > 0 ? (
        validations.map((item, index) => {
          const issues = Array.isArray(item.issues) ? groupValidationIssues(item.issues) : []
          return (
            <div className="validation-group-card" key={index}>
              <div className="validation-group-header">
                <div className="actions">
                  <StatusBadge status={item.validator_kind} />
                  <strong>第 {item.round || 1} 轮验证</strong>
                  <span className="muted-text">{formatTime(item.created_at)}</span>
                </div>
                <StatusBadge status={item.status} />
              </div>
              {issues.length === 0 ? (
                <div className="validation-pass-banner">
                  未发现任何异常或分歧，全部字段均通过该轮校验。
                </div>
              ) : (
                <div className="issue-card-list">
                  {issues.map((issue, idx) => (
                    <div
                      key={idx}
                      className={`issue-card severity-${issueSeverity(issue).toLowerCase()}`}
                    >
                      <StatusBadge status={issueSeverity(issue)} />
                      <div>
                        <strong>
                          {issue.field_en ? `字段 ${issue.field_en}` : '整表提示'} · {issueLabel(issue.code)}
                        </strong>
                        <p>{friendlyError(issue.message || '请对照原文确认该字段的识别结果。')}</p>
                        {issue.field_en && fields.some((field) => field.field_en === issue.field_en) && (
                          <button
                            type="button"
                            className="button-link"
                            onClick={() => onLocateField(issue.field_en)}
                          >
                            定位并处理这个字段 →
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )
        })
      ) : (
        <div className="empty">当前表暂无验证记录。</div>
      )}
    </div>
  )
}
