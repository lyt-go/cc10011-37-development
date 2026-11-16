import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/qualityscore'

type Row = Record<string, string | number | boolean | null>
type PagePayload = { items: Row[]; total: number }

const ruleColumns = [
  '规则编号', '数据集名称', '完整性权重', '准确性权重', '时效性权重', '一致性权重',
  '评分周期', '总分数', '规则版本', '是否生效版本',
]
const ruleActions = ['提交规则评审', '启用规则', '停用规则']
const periods = ['2026-W46', '2026-W47']
const datasets = ['销售汇总数据集', '客流汇总数据集', '库存汇总数据集']

const emptyRuleForm = {
  规则编号: '',
  数据集名称: datasets[0],
  完整性权重: '0.3',
  准确性权重: '0.3',
  时效性权重: '0.2',
  一致性权重: '0.2',
  评分周期: periods[0],
  总分数: '60',
}

type TabKey = 'rules' | 'snapshots' | 'audits'
const tabs: { key: TabKey; label: string }[] = [
  { key: 'rules', label: '评分规则' },
  { key: 'snapshots', label: '评分快照' },
  { key: 'audits', label: '准入审计' },
]

export default function QualityscoreView() {
  const [tab, setTab] = useState<TabKey>('rules')
  const [rules, setRules] = useState<Row[]>([])
  const [snapshots, setSnapshots] = useState<Row[]>([])
  const [audits, setAudits] = useState<Row[]>([])
  const [trace, setTrace] = useState<Record<string, unknown> | null>(null)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [ruleForm, setRuleForm] = useState<Record<string, string>>(emptyRuleForm)
  const [genForm, setGenForm] = useState({ dataset: datasets[0], period: periods[0] })
  const [detailId, setDetailId] = useState<number | null>(null)

  const loadRules = useCallback(async () => {
    const response = await request(`${ENDPOINT}?size=100`)
    if (!response.ok) throw new Error('评分规则列表读取失败')
    const payload = (await response.json()) as PagePayload
    setRules(payload.items ?? [])
  }, [])

  const loadSnapshots = useCallback(async () => {
    const response = await request(`${ENDPOINT}/snapshots?size=100`)
    if (!response.ok) throw new Error('评分快照列表读取失败')
    const payload = (await response.json()) as PagePayload
    setSnapshots(payload.items ?? [])
  }, [])

  const loadAudits = useCallback(async () => {
    const response = await request(`${ENDPOINT}/audits?size=100`)
    if (!response.ok) throw new Error('准入审计读取失败')
    const payload = (await response.json()) as PagePayload
    setAudits(payload.items ?? [])
  }, [])

  const reloadAll = useCallback(async () => {
    setErrorMessage('')
    try {
      await Promise.all([loadRules(), loadSnapshots(), loadAudits()])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据质量分读取失败')
    }
  }, [loadRules, loadSnapshots, loadAudits])

  useEffect(() => {
    void reloadAll()
  }, [reloadAll])

  const stats = useMemo(() => [
    { label: '评分规则总数', value: rules.length },
    { label: '唯一生效版本', value: rules.filter((row) => row['是否生效版本']).length },
    { label: '有效评分快照', value: snapshots.filter((row) => row['是否有效']).length },
    { label: '准入阻断次数', value: audits.filter((row) => !row['是否通过']).length },
  ], [rules, snapshots, audits])

  async function runRuleAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) {
      setErrorMessage(payload.message ?? '规则动作未生效')
      return
    }
    setNotice(payload.message)
    await reloadAll()
  }

  async function createRule(event: React.FormEvent) {
    event.preventDefault()
    setErrorMessage('')
    setNotice('')
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: ruleForm }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      setErrorMessage(payload.message ?? '评分规则登记失败')
      return
    }
    setNotice('评分规则已登记，新版本默认不生效，需点「启用规则」成为唯一生效版本')
    setRuleForm(emptyRuleForm)
    await reloadAll()
  }

  async function generateSnapshot(event: React.FormEvent) {
    event.preventDefault()
    setErrorMessage('')
    setNotice('')
    const response = await request(`${ENDPOINT}/snapshots/generate`, {
      method: 'POST',
      body: JSON.stringify({ values: genForm }),
    })
    const payload = await response.json()
    setNotice(payload.message)
    if (!payload.ok) setErrorMessage(payload.message)
    await reloadAll()
  }

  async function traceGap(dataset: string) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/trace/gap?dataset=${encodeURIComponent(dataset)}`)
      if (!response.ok) throw new Error('调用链追溯失败')
      setTrace(await response.json())
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '调用链追溯失败')
    }
  }

  const detailSnapshot = snapshots.find((row) => Number(row.id) === detailId) ?? null

  return (
    <section className="page" data-module="qualityscore">
      <header className="page-head">
        <div>
          <h2>数据质量分管理</h2>
          <p className="page-desc">
            维护完整性/准确性/时效性/一致性权重，按数据集与评分周期生成可追溯快照，
            快照作为数据服务发布与调用的唯一准入依据。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出评分规则清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <nav className="tab-bar">
        {tabs.map((item) => (
          <button
            key={item.key}
            type="button"
            className={`tab-item ${tab === item.key ? 'active' : ''}`}
            onClick={() => setTab(item.key)}
          >
            {item.label}
          </button>
        ))}
      </nav>

      {notice && <p className="notice-text">{notice}</p>}
      {errorMessage && <p className="error-text">{errorMessage}</p>}

      {tab === 'rules' && (
        <>
          <form className="editor-bar" onSubmit={createRule}>
            {Object.keys(emptyRuleForm).map((field) => (
              <label key={field} className="filter-item">
                <span>{field}</span>
                {field === '数据集名称' ? (
                  <select value={ruleForm[field]} onChange={(e) => setRuleForm((p) => ({ ...p, [field]: e.target.value }))}>
                    {datasets.map((name) => <option key={name}>{name}</option>)}
                  </select>
                ) : field === '评分周期' ? (
                  <select value={ruleForm[field]} onChange={(e) => setRuleForm((p) => ({ ...p, [field]: e.target.value }))}>
                    {periods.map((name) => <option key={name}>{name}</option>)}
                  </select>
                ) : (
                  <input
                    value={ruleForm[field]}
                    onChange={(e) => setRuleForm((p) => ({ ...p, [field]: e.target.value }))}
                  />
                )}
              </label>
            ))}
            <button className="btn primary" type="submit">登记规则新版本</button>
          </form>

          <table className="data-table">
            <thead>
              <tr>
                {ruleColumns.map((column) => <th key={column}>{column}</th>)}
                <th>状态</th>
                <th>可执行动作</th>
              </tr>
            </thead>
            <tbody>
              {rules.map((row) => (
                <tr key={String(row.id)} className={row['是否生效版本'] ? 'row-active' : ''}>
                  {ruleColumns.map((column) => (
                    <td key={column}>{column === '是否生效版本' ? (row[column] ? '是' : '否') : (row[column] ?? '—')}</td>
                  ))}
                  <td><span className={`badge ${row.status === '已启用' ? 'badge-ok' : row.status === '已停用' ? 'badge-bad' : ''}`}>{String(row.status)}</span></td>
                  <td className="row-actions">
                    {ruleActions.map((action) => (
                      <button key={action} className="link" type="button" onClick={() => void runRuleAction(action, row)}>
                        {action}
                      </button>
                    ))}
                  </td>
                </tr>
              ))}
              {rules.length === 0 && (
                <tr><td className="empty-state" colSpan={ruleColumns.length + 2}>暂无评分规则</td></tr>
              )}
            </tbody>
          </table>
          <p className="page-desc">同一数据集并发启停时只允许一个规则版本生效；版本切换或停用后，历史快照立即失效。</p>
        </>
      )}

      {tab === 'snapshots' && (
        <>
          <form className="editor-bar" onSubmit={generateSnapshot}>
            <label className="filter-item">
              <span>数据集名称</span>
              <select value={genForm.dataset} onChange={(e) => setGenForm((p) => ({ ...p, dataset: e.target.value }))}>
                {datasets.map((name) => <option key={name}>{name}</option>)}
              </select>
            </label>
            <label className="filter-item">
              <span>评分周期</span>
              <select value={genForm.period} onChange={(e) => setGenForm((p) => ({ ...p, period: e.target.value }))}>
                {periods.map((name) => <option key={name}>{name}</option>)}
              </select>
            </label>
            <button className="btn primary" type="submit">按完整阶段生成快照</button>
            <button className="btn ghost" type="button" onClick={() => void traceGap(genForm.dataset)}>
              沿调用链查数据缺口
            </button>
          </form>

          {trace && (
            <div className="trace-panel">
              <strong>{String(trace.reason)}</strong>
              <ol className="trace-chain">
                {(trace.chain as { node: string; present: boolean; detail: string }[]).map((node) => (
                  <li key={node.node} className={node.present ? 'trace-ok' : 'trace-gap'}>
                    {node.present ? '✓' : '✗'} {node.node}：{node.detail}
                  </li>
                ))}
              </ol>
            </div>
          )}

          <table className="data-table">
            <thead>
              <tr>
                <th>快照编号</th><th>数据集名称</th><th>评分周期</th><th>规则版本</th>
                <th>总分数</th><th>阶段状态</th><th>已完成阶段数</th><th>是否有效</th><th>失效原因</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {snapshots.map((row) => (
                <tr key={String(row.id)}>
                  <td>{String(row['快照编号'])}</td>
                  <td>{String(row['数据集名称'])}</td>
                  <td>{String(row['评分周期'])}</td>
                  <td>v{String(row['规则版本'])}</td>
                  <td>{String(row['总分数'])}</td>
                  <td><span className={`badge ${row['阶段状态'] === '已完成' ? 'badge-ok' : 'badge-warn'}`}>{String(row['阶段状态'])}</span></td>
                  <td>{(row['已完成阶段'] as unknown[] | null)?.length ?? 0}/5</td>
                  <td>{row['是否有效'] ? '有效' : '已失效'}</td>
                  <td>{row['失效原因'] ? String(row['失效原因']) : '—'}</td>
                  <td className="row-actions">
                    <button className="link" type="button" onClick={() => setDetailId(Number(row.id))}>维度明细</button>
                    <button className="link" type="button" onClick={() => void traceGap(String(row['数据集名称']))}>缺口追溯</button>
                  </td>
                </tr>
              ))}
              {snapshots.length === 0 && (
                <tr><td className="empty-state" colSpan={10}>暂无评分快照，选择数据集与周期后生成；同周期重复计算只形成一份结果</td></tr>
              )}
            </tbody>
          </table>

          {detailSnapshot && (
            <div className="modal-mask" onClick={() => setDetailId(null)}>
              <div className="modal-card" onClick={(e) => e.stopPropagation()}>
                <h3>快照 {String(detailSnapshot['快照编号'])} 维度得分明细</h3>
                <p className="page-desc">
                  {String(detailSnapshot['数据集名称'])} · 评分周期 {String(detailSnapshot['评分周期'])} ·
                  总分 {String(detailSnapshot['总分数'])}
                </p>
                <table className="data-table">
                  <thead><tr><th>维度</th><th>原始分</th><th>权重快照</th><th>加权得分</th></tr></thead>
                  <tbody>
                    {((detailSnapshot['加权明细'] as unknown) as { 维度: string; 原始分: number; 权重: number; 加权得分: number }[]).map((item) => (
                      <tr key={item.维度}>
                        <td>{item.维度}</td><td>{item.原始分}</td><td>{item.权重}</td><td>{item.加权得分}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                <p className="page-desc">完整阶段：{String(((detailSnapshot['已完成阶段'] as unknown) as string[]).join(' → '))}</p>
                <div className="page-actions"><button className="btn" type="button" onClick={() => setDetailId(null)}>关闭</button></div>
              </div>
            </div>
          )}
        </>
      )}

      {tab === 'audits' && (
        <table className="data-table">
          <thead>
            <tr>
              <th>判定时间</th><th>审计场景</th><th>服务编号</th><th>数据集名称</th><th>评分周期</th>
              <th>快照编号</th><th>总分/门槛</th><th>结论</th><th>阻断原因</th>
            </tr>
          </thead>
          <tbody>
            {audits.map((row) => (
              <tr key={String(row.id)}>
                <td>{String(row['判定时间'])}</td>
                <td>{String(row['审计场景'])}</td>
                <td>{row['服务编号'] ? String(row['服务编号']) : '—'}</td>
                <td>{String(row['数据集名称'])}</td>
                <td>{row['评分周期'] ? String(row['评分周期']) : '—'}</td>
                <td>{row['快照编号'] ? `SNAP-${String(row['快照编号']).padStart(4, '0')}` : '—'}</td>
                <td>{row['总分数'] == null ? '—' : `${String(row['总分数'])} / ${String(row['准入门槛'])}`}</td>
                <td><span className={`badge ${row['是否通过'] ? 'badge-ok' : 'badge-bad'}`}>{row['是否通过'] ? '通过' : '阻断'}</span></td>
                <td>{row['阻断原因'] ? String(row['阻断原因']) : '—'}</td>
              </tr>
            ))}
            {audits.length === 0 && (
              <tr><td className="empty-state" colSpan={9}>暂无准入审计，发布或调用数据服务后自动留痕</td></tr>
            )}
          </tbody>
        </table>
      )}
    </section>
  )
}
