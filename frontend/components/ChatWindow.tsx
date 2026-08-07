import ReactMarkdown from "react-markdown";

export function ChatWindow({ messages }: any) {
  return (
    <div className="flex-1 overflow-y-auto p-6 space-y-4">
      {messages.map((msg: any, i: number) => (
        <div key={i} className={msg.role === "user" ? "text-right" : "text-left"}>
          <div className="inline-block px-4 py-3 rounded-xl bg-[#1e1e1e] max-w-xl">
            <ReactMarkdown>{msg.content}</ReactMarkdown>
          </div>
        </div>
      ))}
    </div>
  );
}
