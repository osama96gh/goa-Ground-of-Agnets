import { useEffect, useState } from "react";
import { Download } from "lucide-react";
import type { Attachment } from "@/lib/types";
import { fetchBlobObjectUrl, fetchBlobText } from "@/api/blobs";
import { formatBytes } from "@/lib/format";
import { Button } from "@/components/ui/button";

interface Props {
  attachments: Attachment[];
}

export function AttachmentList({ attachments }: Props) {
  if (!attachments || attachments.length === 0) return null;
  return (
    <div className="mt-2 flex flex-col gap-2">
      {attachments.map((att) => (
        <AttachmentRow key={att.blob_id} attachment={att} />
      ))}
    </div>
  );
}

function AttachmentRow({ attachment }: { attachment: Attachment }) {
  const isImage = attachment.mime_type.startsWith("image/");
  const isText =
    attachment.mime_type.startsWith("text/") ||
    attachment.mime_type === "application/json";
  const inlineableSize = attachment.size_bytes <= 64 * 1024;

  if (isImage) return <ImagePreview attachment={attachment} />;
  if (isText && inlineableSize) return <TextPreview attachment={attachment} />;
  return <FileRow attachment={attachment} />;
}

function ImagePreview({ attachment }: { attachment: Attachment }) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let created: string | null = null;
    fetchBlobObjectUrl(attachment.blob_id)
      .then((u) => {
        if (cancelled) {
          URL.revokeObjectURL(u);
          return;
        }
        created = u;
        setUrl(u);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [attachment.blob_id]);

  return (
    <div className="rounded-md border bg-muted/50 p-2">
      <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
        <span className="truncate">{attachment.filename}</span>
        <span className="ml-2 shrink-0">{formatBytes(attachment.size_bytes)}</span>
      </div>
      {error ? (
        <div className="text-xs text-destructive">failed to load: {error}</div>
      ) : url ? (
        <a href={url} target="_blank" rel="noopener noreferrer">
          <img
            src={url}
            alt={attachment.filename}
            loading="lazy"
            className="max-h-64 max-w-full rounded"
          />
        </a>
      ) : (
        <div className="text-xs text-muted-foreground">loading…</div>
      )}
    </div>
  );
}

function TextPreview({ attachment }: { attachment: Attachment }) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchBlobText(attachment.blob_id)
      .then((t) => {
        if (!cancelled) setText(t);
      })
      .catch((e: Error) => {
        if (!cancelled) setError(e.message);
      });
    return () => {
      cancelled = true;
    };
  }, [attachment.blob_id]);

  return (
    <div className="rounded-md border bg-muted/50 p-2">
      <div className="mb-1 flex items-center justify-between text-xs text-muted-foreground">
        <span className="truncate">{attachment.filename}</span>
        <span className="ml-2 shrink-0">
          {attachment.mime_type} · {formatBytes(attachment.size_bytes)}
        </span>
      </div>
      {error ? (
        <div className="text-xs text-destructive">failed to load: {error}</div>
      ) : text === null ? (
        <div className="text-xs text-muted-foreground">loading…</div>
      ) : (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded bg-background p-2 text-xs">
          {text}
        </pre>
      )}
    </div>
  );
}

function FileRow({ attachment }: { attachment: Attachment }) {
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  useEffect(() => {
    return () => {
      if (downloadUrl) URL.revokeObjectURL(downloadUrl);
    };
  }, [downloadUrl]);

  async function onDownload() {
    setDownloading(true);
    setError(null);
    try {
      const url = await fetchBlobObjectUrl(attachment.blob_id);
      setDownloadUrl(url);
      const a = document.createElement("a");
      a.href = url;
      a.download = attachment.filename;
      a.click();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDownloading(false);
    }
  }

  return (
    <div className="flex items-center justify-between gap-2 rounded-md border bg-muted/50 p-2 text-xs">
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium text-foreground">
          {attachment.filename}
        </div>
        <div className="text-muted-foreground">
          {attachment.mime_type} · {formatBytes(attachment.size_bytes)}
        </div>
      </div>
      <Button
        variant="outline"
        size="sm"
        onClick={onDownload}
        disabled={downloading}
      >
        <Download className="h-3.5 w-3.5" />
        {downloading ? "…" : "Download"}
      </Button>
      {error && <span className="ml-2 text-destructive">{error}</span>}
    </div>
  );
}
