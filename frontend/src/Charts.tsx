import { useState } from "react";
export function OrderChart({
  orders,
}: {
  orders: { create_time?: number | null }[];
}) {
  const [hover, setHover] = useState<number | null>(null);
  const today = new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
  const end = Date.parse(today + "T00:00:00+08:00");
  const days = Array.from({ length: 7 }, (_, i) => {
    const start = end - (6 - i) * 86400000;
    return {
      label: new Date(start + 8 * 3600000)
        .toISOString()
        .slice(5, 10)
        .replace("-", "/"),
      count: orders.filter(
        (o) =>
          o.create_time != null &&
          o.create_time * 1000 >= start &&
          o.create_time * 1000 < start + 86400000,
      ).length,
    };
  });
  const max = Math.max(...days.map((d) => d.count), 1),
    ceil = Math.max(4, Math.ceil(max / 4) * 4),
    width = 720,
    height = 210,
    left = 34,
    right = 12,
    top = 18,
    bottom = 30,
    cw = width - left - right,
    ch = height - top - bottom;
  const x = (i: number) => left + ((i + 0.5) * cw) / 7,
    y = (n: number) => top + ch * (1 - n / ceil);
  const line = days
    .map((d, i) => `${i === 0 ? "M" : "L"} ${x(i)} ${y(d.count)}`)
    .join(" ");
  return (
    <div className="order-chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={
          "近7天已保存订单创建日期分布：" +
          days.map((d) => d.label + " " + d.count + "笔").join("，")
        }
      >
        <defs>
          <linearGradient id="order-area" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#169a80" stopOpacity=".15" />
            <stop offset="100%" stopColor="#169a80" stopOpacity=".01" />
          </linearGradient>
        </defs>
        {[0, 1, 2, 3, 4].map((i) => (
          <g key={i}>
            <line
              x1={left}
              x2={width - right}
              y1={y((i * ceil) / 4)}
              y2={y((i * ceil) / 4)}
              stroke="#edf0f2"
              strokeDasharray={i ? "3 4" : undefined}
            />
            <text
              x={left - 12}
              y={y((i * ceil) / 4) + 4}
              textAnchor="end"
              fill="#929aa4"
              fontSize="10"
            >
              {(i * ceil) / 4}
            </text>
          </g>
        ))}
        <path
          d={`${line} L ${x(6)} ${y(0)} L ${x(0)} ${y(0)} Z`}
          fill="url(#order-area)"
        />
        <path
          d={line}
          fill="none"
          stroke="#128b73"
          strokeWidth="2.5"
          strokeLinejoin="round"
        />
        {days.map((d, i) => (
          <g
            key={i}
            onMouseEnter={() => setHover(i)}
            onMouseLeave={() => setHover(null)}
          >
            <rect
              x={left + (i * cw) / 7}
              width={cw / 7}
              y={0}
              height={height}
              fill="transparent"
            />
            <circle
              cx={x(i)}
              cy={y(d.count)}
              r={hover === i ? 5 : 3.5}
              fill="white"
              stroke="#128b73"
              strokeWidth="2"
            />
            <text
              x={x(i)}
              y={height - 8}
              textAnchor="middle"
              fill="#929aa4"
              fontSize="10"
            >
              {d.label}
            </text>
            {hover === i && (
              <g>
                <rect
                  x={Math.min(x(i) - 30, width - 74)}
                  y={Math.max(y(d.count) - 34, 0)}
                  width="64"
                  height="25"
                  rx="5"
                  fill="#21332e"
                />
                <text
                  x={Math.min(x(i) + 2, width - 42)}
                  y={Math.max(y(d.count) - 17, 17)}
                  fill="white"
                  fontSize="11"
                  textAnchor="middle"
                >
                  {d.count} 笔
                </text>
              </g>
            )}
          </g>
        ))}
      </svg>
    </div>
  );
}
