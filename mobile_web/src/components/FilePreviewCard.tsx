import type { FilePreview } from "../types";
import { useLanguage } from "../i18n";

interface Props {
  preview: FilePreview;
  onClose: () => void;
}

/** Server-rendered preview (text, table, image or a readable fallback). */
export function FilePreviewCard({ preview, onClose }: Props) {
  const { t } = useLanguage();
  return (
    <div className="card">
      <div className="row between">
        <h3>{preview.name}</h3>
        <button className="btn small ghost" onClick={onClose}>
          {t("close")}
        </button>
      </div>
      {preview.error ? <p className="muted">{preview.error}</p> : null}
      {preview.data_uri ? (
        <img className="preview-image" src={preview.data_uri} alt={preview.name} />
      ) : null}
      {preview.text ? <div className="code">{preview.text}</div> : null}
      {preview.rows && preview.rows.length > 0 ? (
        <div className="preview-table-wrap">
          <table className="preview-table">
            <tbody>
              {preview.rows.map((row, rowIndex) => (
                <tr key={rowIndex}>
                  {row.map((cell, cellIndex) => (
                    <td key={cellIndex}>{cell}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {preview.message && !preview.text && !preview.data_uri ? (
        <p className="muted">{preview.message}</p>
      ) : null}
      {preview.sheets && preview.sheets.length > 1 ? (
        <p className="muted">
          {t("sheetLabel")}: {preview.sheets[0]}
        </p>
      ) : null}
      {preview.truncated ? <p className="muted">{t("partialPreview")}</p> : null}
    </div>
  );
}
