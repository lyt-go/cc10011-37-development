import { useCallback, useEffect, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/dataservice'
const columns = [
  '服务编号',
  '服务名称',
  '接口路径',
  '数据集名称',
  '缓存策略',
  '服务版本',
  '绑定快照编号',
  '绑定评分周期',
  '快照状态',
  '准入结论',
]
const actions = ['提交服务评审', '上线服务', '下线服务']
const datasets = ['销售汇总数据集', '客流汇总数据集', '库存汇总数据集']

type Row = Record<string, string | number | boolean | null>
type InvokeResult = Record<string, string | number | Record<string, number> | null>

export default function DataserviceView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [createForm, setCreateForm] = useState<Record<string, string>>({
    服务编号: '', 服务名称: '', 接口路径: '', 数据集名称: datasets[0],
  })
  const [invokeResult, setInvokeResult] = useState<InvokeResult | null>(null)

  const reload = useCallback(async () => {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}?size=100`)
      if (!response.ok) throw new Error('服务接口列表读取失败')
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据服务列表读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    setNotice('')
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      // 准入链阻断信息直接透出：服务列表不再自行解释门槛，只展示后端同一份判定
      setErrorMessage(payload.message ?? '数据服务动作未生效')
    } else {
      setNotice(payload.message)
    }
    await reload()
  }

  async function createService(event: React.FormEvent) {
    event.preventDefault()
    setErrorMessage('')
    setNotice('')
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: createForm }),
    })
    const payload = await response.json()
    if (!payload.ok) {
      setErrorMessage(payload.message ?? '服务接口登记失败')
      return
    }
    setNotice('服务接口已登记，上线时必须通过质量评分准入链并绑定有效快照')
    setCreateForm({ 服务编号: '', 服务名称: '', 接口路径: '', 数据集名称: datasets[0] })
    await reload()
  }

  async function invokeService(row: Row) {
    setErrorMessage('')
    setNotice('')
    setInvokeResult(null)
    const response = await request(`${ENDPOINT}/${row.id}/invoke`, { method: 'POST' })
    const payload = await response.json()
    if (!payload.ok) {
      setErrorMessage(payload.message ?? '服务调用失败')
      await reload()
      return
    }
    setNotice(payload.message)
    setInvokeResult(payload.entry)
    await reload()
  }

  return (
    <section className="page" data-module="dataservice">
      <header className="page-head">
        <div>
          <h2>数据服务管理</h2>
          <p className="page-desc">
            服务发布走质量评分准入链：上线必须绑定满足当前门槛的评分快照；
            快照失效后新请求自动阻断，返回结果记录引用的快照与评分周期。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出数据服务清单
          </button>
        </div>
      </header>

      <form className="editor-bar" onSubmit={createService}>
        {Object.keys(createForm).map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            {field === '数据集名称' ? (
              <select
                value={String(createForm[field])}
                onChange={(event) => setCreateForm((prev) => ({ ...prev, [field]: event.target.value }))}
              >
                {datasets.map((name) => <option key={name}>{name}</option>)}
              </select>
            ) : (
              <input
                value={String(createForm[field])}
                onChange={(event) => setCreateForm((prev) => ({ ...prev, [field]: event.target.value }))}
              />
            )}
          </label>
        ))}
        <button className="btn primary" type="submit">登记服务接口</button>
      </form>

      {notice && <p className="notice-text">{notice}</p>}
      {errorMessage && <p className="error-text">{errorMessage}</p>}

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>状态</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] == null || row[column] === '' ? '—' : String(row[column])}</td>)}
              <td><span className={`badge ${row.status === '已上线' ? 'badge-ok' : row.status === '已下线' ? 'badge-bad' : 'badge-warn'}`}>{String(row.status)}</span></td>
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
                {row.status === '已上线' && (
                  <button className="link" type="button" onClick={() => void invokeService(row)}>调用服务</button>
                )}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td className="empty-state" colSpan={columns.length + 2}>暂无数据服务数据</td></tr>
          )}
        </tbody>
      </table>

      {invokeResult && (
        <div className="modal-mask" onClick={() => setInvokeResult(null)}>
          <div className="modal-card" onClick={(event) => event.stopPropagation()}>
            <h3>服务返回结果（含评分快照引用）</h3>
            <table className="data-table">
              <tbody>
                <tr><th>服务编号</th><td>{String(invokeResult['服务编号'])}</td></tr>
                <tr><th>数据集名称</th><td>{String(invokeResult['数据集名称'])}</td></tr>
                <tr><th>评分快照编号</th><td>{String(invokeResult['评分快照编号'])}</td></tr>
                <tr><th>评分周期</th><td>{String(invokeResult['评分周期'])}</td></tr>
                <tr><th>质量总分</th><td>{String(invokeResult['质量总分'])}</td></tr>
                <tr><th>快照是否有效</th><td>{invokeResult['快照是否有效'] ? '有效' : '已失效'}</td></tr>
                <tr><th>准入审计ID</th><td>{String(invokeResult['准入审计ID'])}</td></tr>
                <tr>
                  <th>维度得分</th>
                  <td>{Object.entries(invokeResult['维度得分'] as Record<string, number>).map(([key, value]) => `${key} ${value}`).join('；')}</td>
                </tr>
              </tbody>
            </table>
            <div className="page-actions"><button className="btn" type="button" onClick={() => setInvokeResult(null)}>关闭</button></div>
          </div>
        </div>
      )}

      <footer className="page-foot">
        <span>共 {total} 条数据服务记录；准入结论由质量评分准入链统一判定并审计</span>
      </footer>
    </section>
  )
}
