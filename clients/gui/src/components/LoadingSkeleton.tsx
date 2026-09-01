import React from 'react';

interface LoadingSkeletonProps {
  lines?: number;
  height?: number | string;
  className?: string;
  style?: React.CSSProperties;
}

export const LoadingSkeleton: React.FC<LoadingSkeletonProps> = ({
  lines = 3,
  height = 20,
  style,
}) => {
  return (
    <div
      role="status"
      aria-label="内容加载中"
      style={{ display: 'flex', flexDirection: 'column', gap: 10, width: '100%', ...style }}
    >
      {Array.from({ length: lines }).map((_, i) => (
        <div
          key={i}
          aria-hidden="true"
          style={{
            height: typeof height === 'number' ? `${height}px` : height,
            width: i === lines - 1 && lines > 1 ? '70%' : '100%',
            borderRadius: 'var(--radius-sm)',
            background:
              'linear-gradient(90deg, var(--bg-subtle) 25%, var(--bg-surface) 50%, var(--bg-subtle) 75%)',
            backgroundSize: '200% 100%',
            animation: 'shimmer 1.4s linear infinite',
          }}
        />
      ))}
    </div>
  );
};
