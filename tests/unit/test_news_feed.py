"""news_feed 单元测试：真实新闻注入的时效性/降级契约。

设计意图（针对 D组"新闻情绪"/C组"多空辩论"的幻觉根因）：
  旧链路 decision_input.json 无任何 news 字段，agent 只能凭训练记忆"编新闻"，
  出现"登顶≠信号有效"的假信号。本模块把 akshare 个股新闻(带发布时间+来源+链接)
  结构化注入，并强制标注时效：超 stale_days 或无法解析时间的一律 stale=True，
  取数失败/空返回给出明确 status，让上层能执行"新闻须新鲜+带源才可作买入依据"的闸门。
"""
import datetime as dt
import unittest

from astock.data import news_feed


class _FakeRow(dict):
    """模拟 DataFrame.iterrows() 产出的行（支持 .get）。"""


class _FakeDF:
    """极简 DataFrame 替身：只实现 news_feed 用到的 __len__/head/iterrows。"""
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    def head(self, n):
        return _FakeDF(self._rows[:n])

    def iterrows(self):
        for i, r in enumerate(self._rows):
            yield i, _FakeRow(r)


def _row(title, source, url, published):
    return {"新闻标题": title, "文章来源": source,
            "新闻链接": url, "发布时间": published}


NOW = dt.datetime(2026, 7, 30, 15, 0, 0)


class TestNewsFeed(unittest.TestCase):
    def test_fresh_news_parsed_with_source_and_link(self):
        df = _FakeDF([_row("比亚迪澄清未直投长鑫", "证券时报",
                           "http://x/1", "2026-07-29 12:44:23")])
        res = news_feed.get_stock_news("002594", now=NOW, fetch=lambda c: df)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["fresh_count"], 1)
        item = res["items"][0]
        self.assertEqual(item["title"], "比亚迪澄清未直投长鑫")
        self.assertEqual(item["source"], "证券时报")
        self.assertEqual(item["url"], "http://x/1")
        self.assertEqual(item["published"], "2026-07-29 12:44")
        self.assertFalse(item["stale"])
        self.assertEqual(item["age_days"], 1)

    def test_old_news_flagged_stale(self):
        df = _FakeDF([_row("半年前的旧闻", "东财", "http://x/2",
                           "2026-07-20 09:00:00")])  # 10 天前 > 默认 3 天
        res = news_feed.get_stock_news("002594", now=NOW, fetch=lambda c: df)
        self.assertEqual(res["status"], "stale_only")
        self.assertEqual(res["fresh_count"], 0)
        self.assertTrue(res["items"][0]["stale"])

    def test_unparseable_time_is_stale_not_crash(self):
        df = _FakeDF([_row("时间字段异常", "来源", "http://x/3", "昨天")])
        res = news_feed.get_stock_news("002594", now=NOW, fetch=lambda c: df)
        self.assertTrue(res["items"][0]["stale"])
        self.assertIsNone(res["items"][0]["published"])
        self.assertIsNone(res["items"][0]["age_days"])

    def test_fetch_failure_returns_unavailable_not_raise(self):
        def boom(code):
            raise RuntimeError("network down")
        res = news_feed.get_stock_news("002594", now=NOW, fetch=boom)
        self.assertEqual(res["status"], "unavailable")
        self.assertEqual(res["items"], [])
        self.assertIn("network down", res["reason"])

    def test_empty_dataframe_returns_empty(self):
        res = news_feed.get_stock_news("002594", now=NOW, fetch=lambda c: _FakeDF([]))
        self.assertEqual(res["status"], "empty")
        self.assertEqual(res["items"], [])

    def test_respects_max_items_cap(self):
        rows = [_row(f"news{i}", "s", f"http://x/{i}", "2026-07-29 10:00:00")
                for i in range(20)]
        res = news_feed.get_stock_news("002594", max_items=3, now=NOW,
                                       fetch=lambda c: _FakeDF(rows))
        self.assertEqual(len(res["items"]), 3)

    def test_get_news_for_codes_batches_and_caps(self):
        """批量入口：对多票取新闻，去重并限制票数上限，单票失败不拖垮整体。"""
        calls = []

        def fetch(code):
            calls.append(code)
            if code == "BAD":
                raise RuntimeError("boom")
            return _FakeDF([_row(f"{code}-news", "s", "http://x",
                                 "2026-07-29 10:00:00")])

        out = news_feed.get_news_for_codes(
            ["AAA", "AAA", "BAD", "CCC"], max_codes=10, now=NOW, fetch=fetch)
        # 去重后 3 只
        self.assertEqual(set(out.keys()), {"AAA", "BAD", "CCC"})
        self.assertEqual(out["AAA"]["status"], "ok")
        self.assertEqual(out["BAD"]["status"], "unavailable")

    def test_get_news_for_codes_enforces_max_codes(self):
        out = news_feed.get_news_for_codes(
            [f"C{i}" for i in range(30)], max_codes=5, now=NOW,
            fetch=lambda c: _FakeDF([]))
        self.assertLessEqual(len(out), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
