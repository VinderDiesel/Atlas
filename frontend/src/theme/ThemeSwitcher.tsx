/**
 * 主题切换按钮——顶栏右侧日/月图标。
 *
 * 消费 useThemeMode() 上下文，aria-label 标注当前动作（WIG 合规：icon-only 按钮
 * 必须有可访问名称）。
 */
import { MoonOutlined, SunOutlined } from "@ant-design/icons";
import { Button, Tooltip } from "antd";

import { useThemeMode } from "./ThemeProvider";

export default function ThemeSwitcher() {
  const { mode, toggleTheme } = useThemeMode();
  const isDark = mode === "dark";

  return (
    <Tooltip title={isDark ? "切换至浅色主题" : "切换至深色主题"}>
      <Button
        type="text"
        onClick={toggleTheme}
        aria-label={isDark ? "切换至浅色主题" : "切换至深色主题"}
        icon={isDark ? <SunOutlined /> : <MoonOutlined />}
      />
    </Tooltip>
  );
}
