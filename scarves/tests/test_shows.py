"""The show planner — `scarves/shows.py` and `private/shows/`.
"""
from datetime import date, datetime, time
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .. import shows
from ..models import (
    FinishedProduct, RawProduct, RawProductCategory, Sale, SaleLine, Show, ShowCost,
)
from .helpers import make_recipe

D = Decimal


def _show(**extra):
    fields = dict(
        name="Spring Fiber Fest", first_day=date(2027, 4, 10),
        last_day=date(2027, 4, 11), days_open=2,
        sales_per_day=D("1500"), silk_share=40, yarn_share=50,
        silk_price=D("60"), silk_blank=D("15"),
        yarn_price=D("25"), yarn_blank=D("8"),
        bought_price=D("10"), bought_cost=D("7"),
        card_fee_percent=D("2.7"),
    )
    fields.update(extra)
    show = Show.objects.create(**fields)
    ShowCost.objects.create(show=show, label="Booth fee", per=ShowCost.PER_SHOW, amount=D("300"), order=0)
    ShowCost.objects.create(show=show, label="Crew", per=ShowCost.PER_DAY, amount=D("400"), order=1)
    return show


class Shop:
    """Silk, a dyed yarn, an undyed yarn and a notion, and a way to sell them."""

    def __init__(self):
        silk = RawProductCategory.objects.get_or_create(name="Silk")[0]
        yarn = RawProductCategory.objects.get_or_create(name="Yarn")[0]
        notions = RawProductCategory.objects.get_or_create(name="Notions")[0]
        self.veil = RawProduct.objects.create(name="Rectangle Veil", category=silk, price="18.00")
        self.homespun = RawProduct.objects.create(name="Homespun", category=yarn, price="6.00")
        self.undyed = RawProduct.objects.create(name="Undyed Yak", category=yarn, price="9.00")
        self.hook = RawProduct.objects.create(name="Crochet Hook", category=notions, price="1.00")
        self.silk = FinishedProduct.objects.create(
            name="Veil - Teal", raw_product=self.veil, recipe=make_recipe("Teal"), price="70.00")
        self.yarn = FinishedProduct.objects.create(
            name="Homespun - Teal", raw_product=self.homespun,
            recipe=FinishedProduct.objects.get(name="Veil - Teal").recipe, price="25.00")
        self.yak = FinishedProduct.objects.create(
            name="Undyed Yak", raw_product=self.undyed, recipe=None, price="20.00")
        self.n = 0

    def sell(self, day, units, net, product=None, raw=None, category=""):
        self.n += 1
        when = timezone.make_aware(datetime.combine(day, time(12)))
        sale = Sale.objects.create(order_id=f"o{self.n}", sold_at=when,
                                   source=Sale.SOURCE_SQUARE_API)
        return SaleLine.objects.create(
            sale=sale, line_key=f"k{self.n}", sold_at=when, category=category,
            item_name="x", quantity=D(units), net_cents=int(net * 100),
            finished_product=product, raw_product=raw,
        )


class PlanTests(TestCase):
    def test_a_piece_sold_is_a_piece_to_make_again(self):
        show = _show(silk_dyeing=D("15"), yarn_dyeing=D("7.50"))
        p = shows.plan(show)
        self.assertEqual(p.sales, D("3000"))
        # $1,200 of silk at $60, $1,500 of yarn at $25, $300 bought in at $10.
        pieces = {g.key: g.pieces for g in p.groups}
        self.assertEqual(pieces, {"silk": 20, "yarn": 60, "bought": 30})
        self.assertEqual(p.dyed_pieces, 80)
        self.assertEqual(p.blanks, 20 * 15 + 60 * 8 + 30 * 7)
        self.assertEqual(p.dyeing, 20 * 15 + 60 * D("7.5"))
        self.assertEqual(p.card, D("81"))
        self.assertEqual(p.being_there, 300 + 400 * 2)
        self.assertEqual(p.left, 3000 - (990 + 750 + 81) - 1100)
        self.assertFalse(p.dyeing_unset)

    def test_an_empty_dyeing_rate_is_flagged_not_quietly_zero(self):
        show = _show(silk_dyeing=None, yarn_dyeing=D("7.50"))
        p = shows.plan(show)
        self.assertTrue(p.dyeing_unset)
        self.assertEqual(p.dyeing, 60 * D("7.5"))

    def test_no_dyed_pieces_needs_no_rate(self):
        show = _show(silk_share=0, yarn_share=0)
        self.assertFalse(shows.plan(show).dyeing_unset)

    def test_mileage_is_the_round_trip_times_the_trips(self):
        show = _show(round_trip_miles=120, trips=2)
        ShowCost.objects.create(show=show, label="Mileage", per=ShowCost.PER_MILE, amount=D("0.70"), order=2)
        line = shows.plan(show).costs[-1]
        self.assertEqual(line.amount, D("168"))
        self.assertIn("240 miles", line.note)

    def test_break_even_is_being_there_over_what_a_dollar_keeps(self):
        show = _show(silk_dyeing=D("15"), yarn_dyeing=D("7.50"))
        keep = shows.keep_share(show)
        # At break-even the plan leaves nothing.
        at = shows.break_even_per_day(show)
        show.sales_per_day = at
        self.assertAlmostEqual(float(shows.plan(show).left), 0, places=6)
        self.assertAlmostEqual(float(at), 1100 / float(keep) / 2, places=6)

    def test_no_break_even_when_a_piece_costs_more_than_it_sells_for(self):
        show = _show(silk_share=100, yarn_share=0, silk_blank=D("50"), silk_dyeing=D("15"))
        self.assertIsNone(shows.break_even_per_day(show))


class GroupTests(TestCase):
    def setUp(self):
        self.shop = Shop()

    def group(self, **kw):
        return shows.group_of(self.shop.sell(date(2027, 4, 10), 1, 1, **kw))

    def test_dyed_colorways_go_by_their_blank(self):
        self.assertEqual(self.group(product=self.shop.silk), "silk")
        self.assertEqual(self.group(product=self.shop.yarn), "yarn")

    def test_a_passthrough_is_bought_in_whatever_its_category(self):
        self.assertEqual(self.group(product=self.shop.yak), "bought")

    def test_silk_rung_without_a_colour_is_still_silk(self):
        self.assertEqual(self.group(raw=self.shop.veil), "silk")

    def test_a_notion_is_bought_in(self):
        self.assertEqual(self.group(raw=self.shop.hook), "bought")

    def test_an_unmatched_line_falls_back_to_squares_category(self):
        self.assertEqual(self.group(category="Silk Scarves"), "silk")
        self.assertEqual(self.group(category="Yarn"), "yarn")
        self.assertEqual(self.group(category="Notions"), "bought")


class ActualTests(TestCase):
    def setUp(self):
        self.shop = Shop()
        self.show = _show(decision=Show.GOING, silk_dyeing=D("15"), yarn_dyeing=D("7.50"))

    def test_reads_the_ledger_over_the_shows_dates(self):
        self.shop.sell(date(2027, 4, 10), 2, 140, product=self.shop.silk)
        self.shop.sell(date(2027, 4, 11), 4, 100, product=self.shop.yarn)
        self.shop.sell(date(2027, 4, 12), 9, 900, product=self.shop.yarn)  # the day after
        a = shows.actual(self.show)
        self.assertEqual(a.sales, D("240"))
        self.assertEqual(a.pieces, 6)
        self.assertEqual(a.blanks, 2 * 18 + 4 * 6)
        self.assertEqual(a.dyeing, 2 * 15 + 4 * D("7.5"))

    def test_a_candidate_never_reads_the_weekend(self):
        self.shop.sell(date(2027, 4, 10), 2, 140, product=self.shop.silk)
        self.show.decision = Show.CONSIDERING
        self.assertIsNone(shows.actual(self.show))

    def test_nothing_on_file_is_no_reading_not_zero(self):
        self.assertIsNone(shows.actual(self.show))

    def test_a_cost_with_no_actual_carries_its_plan_and_says_so(self):
        self.shop.sell(date(2027, 4, 10), 1, 70, product=self.shop.silk)
        crew = self.show.costs.get(label="Crew")
        crew.actual = D("650")
        crew.save()
        a = shows.actual(self.show)
        self.assertEqual([(c.amount, c.as_planned) for c in a.costs],
                         [(D("300"), True), (D("650"), False)])
        self.assertEqual(a.as_planned, 1)

    def test_a_piece_with_no_blank_cost_is_priced_at_the_plan_and_counted(self):
        self.shop.sell(date(2027, 4, 10), 3, 180, category="Silk Scarves")
        g = shows.actual(self.show).groups[0]
        self.assertEqual((g.key, g.estimated, g.blanks), ("silk", 3, 3 * 15))


class MakeTests(TestCase):
    def test_seeds_from_the_last_season_and_leaves_dyeing_empty(self):
        call_command("generate_faire", "--year", "2026", stdout=StringIO())
        shop = Shop()
        shop.sell(date(2026, 8, 29), 1, 70, product=shop.silk)
        shop.sell(date(2026, 8, 29), 4, 100, product=shop.yarn)
        shop.sell(date(2026, 8, 30), 3, 30, raw=shop.hook)
        show = shows.make(name="X", first_day=date(2027, 5, 1),
                          last_day=date(2027, 5, 2), days_open=2)
        self.assertEqual((show.silk_share, show.yarn_share), (35, 50))
        self.assertEqual((show.silk_price, show.silk_blank), (D("70.00"), D("18.00")))
        self.assertEqual((show.yarn_price, show.yarn_blank), (D("25.00"), D("6.00")))
        self.assertIsNone(show.silk_dyeing)
        self.assertIsNone(show.yarn_dyeing)
        self.assertEqual([c.label for c in show.costs.all()],
                         [label for label, _, _ in shows.DEFAULT_COSTS])

    def test_with_no_season_on_file_it_starts_empty(self):
        show = shows.make(name="X", first_day=date(2027, 5, 1),
                          last_day=date(2027, 5, 1), days_open=1)
        self.assertEqual(show.silk_price, 0)


class PageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("staff", password="x")
        self.client.force_login(self.user)

    def test_starting_a_show_lands_on_its_page(self):
        response = self.client.post(reverse("shows_index"), {
            "name": "Fiber Fest", "first_day": "2027-04-10",
            "last_day": "2027-04-11", "days_open": "2",
        })
        show = Show.objects.get()
        self.assertRedirects(response, reverse("show_detail", kwargs={"show_id": show.pk}))
        self.assertContains(self.client.get(reverse("shows_index")), "Fiber Fest")

    def test_more_days_open_than_the_dates_hold_is_refused(self):
        response = self.client.post(reverse("shows_index"), {
            "name": "Fiber Fest", "first_day": "2027-04-10",
            "last_day": "2027-04-11", "days_open": "3",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Show.objects.exists())

    def test_the_page_says_dyeing_is_not_set(self):
        show = _show()
        response = self.client.get(reverse("show_detail", kwargs={"show_id": show.pk}))
        self.assertContains(response, "before dyeing")
        self.assertContains(response, "a day</strong> just")

    def test_saving_the_plan_and_a_new_cost(self):
        show = _show()
        url = reverse("show_detail", kwargs={"show_id": show.pk})
        form = self.client.get(url).context["form"]
        costs = self.client.get(url).context["costs"]
        data = {k: ("" if v is None else v) for k, v in form.initial.items()
                if k in form.fields}
        data.update({"silk_dyeing": "15", "yarn_dyeing": "7.50", "decision": "going",
                     "first_day": "2027-04-10", "last_day": "2027-04-11"})
        prefix = costs.prefix
        data.update({f"{prefix}-TOTAL_FORMS": "4", f"{prefix}-INITIAL_FORMS": "2",
                     f"{prefix}-MIN_NUM_FORMS": "0", f"{prefix}-MAX_NUM_FORMS": "1000"})
        for i, cost in enumerate(show.costs.all()):
            data.update({f"{prefix}-{i}-id": cost.pk, f"{prefix}-{i}-label": cost.label,
                         f"{prefix}-{i}-per": cost.per, f"{prefix}-{i}-amount": cost.amount,
                         f"{prefix}-{i}-actual": ""})
        data[f"{prefix}-1-DELETE"] = "on"
        data.update({f"{prefix}-2-label": "Motel", f"{prefix}-2-per": "show",
                     f"{prefix}-2-amount": "180", f"{prefix}-2-actual": ""})
        # The last spare row, as a browser posts one nobody touched.
        data.update({f"{prefix}-3-label": "", f"{prefix}-3-per": "show",
                     f"{prefix}-3-amount": "0", f"{prefix}-3-actual": ""})
        response = self.client.post(url, data)
        self.assertRedirects(response, url)
        show.refresh_from_db()
        self.assertEqual(show.silk_dyeing, D("15"))
        self.assertEqual(show.decision, Show.GOING)
        self.assertEqual([c.label for c in show.costs.all()], ["Booth fee", "Motel"])

    def test_pull_runs_the_square_import_over_the_dates_so_far(self):
        show = _show(first_day=date(2026, 10, 3), last_day=date(2026, 10, 30), days_open=4,
                     decision=Show.GOING)
        with mock.patch("scarves.views.shows.timezone.localdate", return_value=date(2026, 10, 20)), \
                mock.patch("scarves.views.shows.call_command") as run:
            self.client.post(reverse("show_pull_sales", kwargs={"show_id": show.pk}))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[1:],
                         ("--from", "2026-10-03", "--to", "2026-10-20"))

    def test_pull_refuses_a_show_that_has_not_started(self):
        show = _show(decision=Show.GOING)
        with mock.patch("scarves.views.shows.call_command") as run:
            self.client.post(reverse("show_pull_sales", kwargs={"show_id": show.pk}))
        run.assert_not_called()
