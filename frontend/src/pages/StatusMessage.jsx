export default function StatusMessage({ error, notice, onClose }) {
  return <>
    {error && <div className="message error" role="alert">{typeof error === 'string' ? error : JSON.stringify(error)}</div>}
    {notice && <div className="message notice">{notice}<button onClick={onClose}>关闭</button></div>}
  </>
}
