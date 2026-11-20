import { EvidenceCard } from '../SharedUI.jsx'

export default function FieldEditorDrawer({
  activeField,
  filteredFields,
  isPublished,
  busy,
  editReason,
  onEditReasonChange,
  onSelectField,
  onChangeProp,
  onSave,
  onRemove,
  onClose,
}) {
  if (!activeField) return null

  const currentIndex = filteredFields.findIndex((field) => field.id === activeField.id)

  return (
    <div className="field-editor-drawer-backdrop" onClick={onClose}>
      <aside
        className="field-editor-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={`编辑字段 ${activeField.field_en}`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="panel-heading field-editor-header">
          <div>
            <h2>
              查看或修改字段：{activeField.field_cn} (<code>{activeField.field_en}</code>)
            </h2>
            <p>修改后请点击保存；字段原文和自动识别依据可在下方查看。</p>
          </div>
          <div className="actions field-editor-actions">
            <button
              type="button"
              className="button secondary sm"
              disabled={currentIndex <= 0}
              onClick={() => {
                if (currentIndex > 0) onSelectField(filteredFields[currentIndex - 1].id)
              }}
            >
              上一个字段
            </button>
            <button
              type="button"
              className="button secondary sm"
              disabled={currentIndex === filteredFields.length - 1}
              onClick={() => {
                if (currentIndex < filteredFields.length - 1) onSelectField(filteredFields[currentIndex + 1].id)
              }}
            >
              下一个字段
            </button>
            {!isPublished && (
              <>
                <input
                  placeholder="修改原因说明..."
                  value={editReason}
                  onChange={(e) => onEditReasonChange(e.target.value)}
                />
                <button
                  type="button"
                  className="button danger sm"
                  disabled={busy}
                  onClick={() => onRemove(activeField)}
                >
                  删除字段
                </button>
                <button
                  type="button"
                  className="button primary sm"
                  disabled={busy}
                  onClick={() => onSave(activeField)}
                >
                  保存修改
                </button>
              </>
            )}
            <button type="button" className="button secondary sm" onClick={onClose}>
              关闭
            </button>
          </div>
        </div>

        <div className="field-editor-grid">
          <label>
            <span>中文名称</span>
            <input disabled={isPublished} value={activeField.field_cn} onChange={(e) => onChangeProp(activeField.id, 'field_cn', e.target.value)} />
          </label>
          <label>
            <span>英文列名</span>
            <input disabled={isPublished} value={activeField.field_en} onChange={(e) => onChangeProp(activeField.id, 'field_en', e.target.value)} />
          </label>
          <label>
            <span>数据类型与长度</span>
            <div className="actions field-type-inputs">
              <select disabled={isPublished} value={activeField.data_type} onChange={(e) => onChangeProp(activeField.id, 'data_type', e.target.value)}>
                <option value="VARCHAR">文本</option>
                <option value="NUMBER">数值</option>
                <option value="DATE">日期</option>
                <option value="CHAR">定长文本</option>
              </select>
              <input type="number" disabled={isPublished} style={{ width: 92 }} value={activeField.length} onChange={(e) => onChangeProp(activeField.id, 'length', e.target.value)} title="字段长度" />
              <input type="number" disabled={isPublished} style={{ width: 76 }} value={activeField.decimal_places} onChange={(e) => onChangeProp(activeField.id, 'decimal_places', e.target.value)} title="小数位数" />
            </div>
          </label>
          <label className="span-2">
            <span>关联码值表</span>
            <input disabled={isPublished} placeholder="无关联码值表可留空" value={activeField.code_reference_text} onChange={(e) => onChangeProp(activeField.id, 'code_reference_text', e.target.value)} />
          </label>
          <label>
            <span>类型依据</span>
            <select
              disabled={isPublished}
              value={String(activeField.type_basis || 'UNKNOWN').toUpperCase()}
              onChange={(e) => onChangeProp(activeField.id, 'type_basis', e.target.value)}
              title="改了类型/长度会自动记为人工确认"
            >
              <option value="SOURCE">原文有依据</option>
              <option value="INFERRED">类型是推断</option>
              <option value="MANUAL">人工确认</option>
              <option value="UNKNOWN">待确认</option>
            </select>
          </label>
          <label className="check-label">
            <input type="checkbox" disabled={isPublished} checked={Boolean(activeField.required)} onChange={(e) => onChangeProp(activeField.id, 'required', e.target.checked)} />
            必填字段
          </label>
          <label className="span-3">
            <span>业务定义</span>
            <textarea disabled={isPublished} value={activeField.field_definition} onChange={(e) => onChangeProp(activeField.id, 'field_definition', e.target.value)} />
          </label>
          <label className="span-3">
            <span>规范中的校验要求</span>
            <textarea disabled={isPublished} value={activeField.check_text_raw} onChange={(e) => onChangeProp(activeField.id, 'check_text_raw', e.target.value)} />
          </label>
        </div>

        <details className="technical-details">
          <summary>查看自动识别初值与原文依据</summary>
          <EvidenceCard field={activeField} />
        </details>
      </aside>
    </div>
  )
}
