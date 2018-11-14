import json
import logging

from datetime import date

from exceptions import NotFoundError, IsVoteError, ErrorCode, DuplicateError
from forms.votes import CandidateForm
from handlers.base import BaseHandler
from mixins import PaginationMixin
from models import objects
from models.votes import Candidate, Vote, VoteEvent, VoteBanner, CandidateImage
from settings import redis
from utils.decorators import async_authenticated
from utils.json import json_serializer

logger = logging.getLogger('vote.' + __name__)


class VoteDetailHandler(BaseHandler):
    """
    投票详情接口
    """
    SUPPORTED_METHODS = ('GET', 'OPTIONS')

    async def get(self, pk, *args, **kwargs):
        try:
            await objects.get(Vote, id=pk)
            query = Vote.get_vote_info_by_pk(pk)

            vote = await objects.execute(query)

            vote = vote[0]
            vote.views += 1
            await objects.execute(Vote.update(views=Vote.views + 1).where(Vote.id == pk))

            banners = await objects.execute(VoteBanner.filter(vote_id=1))
            banners = [{'url': banner.image} for banner in banners]
            ret = dict(
                banners=banners,
                start_time=vote.start_time,
                end_time=vote.end_time,
                views=vote.views,
                announcement=vote.announcement,
                title=vote.title,
                description=vote.description,
                number_of_votes=int(vote.number_of_votes),
                number_of_candidates=vote.number_of_candidates,
            )
            self.finish(json.dumps(ret, default=json_serializer))
        except Vote.DoesNotExist:
            raise NotFoundError("投票活动不存在")


class CandidateListHandler(BaseHandler, PaginationMixin):
    """
    选手列表接口
    """
    SUPPORTED_METHODS = ('GET', 'POST', 'OPTIONS')

    async def get(self, *args, **kwargs):
        ordering = self.get_argument("ordering", None)
        vote_id = self.get_argument('vote_id')
        query = Candidate.query_candidates_by_vote_id(vote_id=vote_id)
        if ordering == '1':
            query = query.order_by(Candidate.create_time.desc())
        elif ordering == '0':
            query = query.order_by(Candidate.number_of_votes.desc())

        page = self.get_paginate_query(query)
        if page is not None:
            query = page
        candidates = objects.execute(query)
        ret = self.get_serializer_data(candidates)
        if page is not None:
            ret = self.get_paginated_response(ret)
        self.finish(json.dumps(ret))

    @staticmethod
    def get_serializer_data(candidates):
        ret = []
        for candidate in candidates:
            ret.append(dict(
                cover=candidate.cover,
                id=candidate.id,
                name=candidate.user.name,
                number=candidate.number,
                number_of_votes=candidate.number_of_votes,
                diff=candidate.diff,
                vote_rank=candidate.vote_rank))
        return ret

    @async_authenticated
    async def post(self, *args, **kwargs):
        param = self.request.body.decode("utf-8")
        data = json.loads(param)
        candidate_form = CandidateForm.from_json(data)
        if candidate_form.validate():
            user = self.current_user
            is_new = True if not user.mobile else False
            vote_id = candidate_form.vote_id.data
            declaration = candidate_form.declaration.data
            images = candidate_form.images.data

            async with objects.database.atomic_async():
                try:
                    await objects.get(Vote, id=vote_id)
                    # 新用户保存姓名跟手机号
                    if is_new:
                        mobile = candidate_form.mobile.data
                        code = candidate_form.code.data
                        redis_key = "{}_{}".format(mobile, code)
                        if not redis.get(redis_key):
                            raise ErrorCode
                        user.mobile = mobile
                        user.name = candidate_form.name.data
                        await objects.update(user)
                    await objects.get(Candidate, user=user, vote_id=vote_id)
                    raise DuplicateError
                except Candidate.DoesNotExist:
                    count = await objects.count(Candidate.select().where(Candidate.vote_id == vote_id))
                    number = "%03d" % (count + 1)
                    candidate = await objects.create(Candidate,
                                                     vote_id=vote_id,
                                                     declaration=declaration,
                                                     cover=images[0],
                                                     number=number,
                                                     user=user)
                    for image in images:
                        await objects.create(CandidateImage,
                                             candidate=candidate,
                                             url=image)

                    self.finish({"candidate_id": candidate.id})

                except Vote.DoesNotExist:
                    raise NotFoundError("投票不存在")


class CandidateDetailHandler(BaseHandler):
    """
    选手详情接口
    """
    SUPPORTED_METHODS = ('GET', 'OPTIONS')

    async def get(self, pk, *args, **kwargs):
        vote_id = self.get_argument('vote_id')
        try:
            await objects.get(Vote, id=vote_id)

            query = Candidate.query_candidates_by_vote_id(vote_id=vote_id).where(Candidate.id == pk)
            candidate = await objects.execute(query)

            candidate = candidate[0]
            images = await objects.execute(CandidateImage.select().where(CandidateImage.candidate_id == candidate.id))
            ret = dict(
                name=candidate.user.name,
                number=candidate.number,
                images=[image.url for image in images],
                declaration=candidate.declaration,
                number_of_votes=candidate.number_of_votes,
                diff=candidate.diff,
                rank=candidate.vote_rank)
            self.finish(json.dumps(ret))
        except IndexError:
            raise NotFoundError("选手不存在")
        except Vote.DoesNotExist:
            raise NotFoundError("投票不存在")


class VoteEventListHandler(BaseHandler, PaginationMixin):
    """
    投票事件流列表接口
    """
    SUPPORTED_METHODS = ('GET', 'OPTIONS')

    async def get(self, candidate_id, *args, **kwargs):
        query = VoteEvent.select().where(VoteEvent.candidate_id == candidate_id)
        page = self.get_paginate_query(query)
        if page is not None:
            query = page
        vote_events = await objects.execute(query)
        ret = self.get_serializer_data(vote_events)
        if page is not None:
            ret = self.get_paginated_response(ret)
        self.finish(json.dumps(ret, default=json_serializer))

    @staticmethod
    def get_serializer_data(vote_events):
        ret = []
        for vote_event in vote_events:
            ret.append(dict(
                voter_avatar=vote_event.voter_avatar,
                voter_nickname=vote_event.voter_nickname,
                reach=vote_event.reach,
                image=vote_event.image,
                is_gift=True if vote_event.gift_id else False,
                number_of_gifts=vote_event.number_of_gifts,
                create_time=vote_event.create_time))
        return ret


class VoteRankListHandler(BaseHandler):
    """
    投票贡献排行
    """
    SUPPORTED_METHODS = ('GET', 'OPTIONS')

    async def get(self, candidate_id, *args, **kwargs):
        query = VoteEvent.get_vote_rank(candidate_id)
        print(query.sql()[0])
        ranks = await objects.execute(query)

        ret = []
        for rank in ranks:
            ret.append(dict(
                voter_avatar=rank.voter_avatar,
                voter_nickname=rank.voter_nickname,
                number_of_votes=int(rank.number_of_votes),
                vote_rank=rank.vote_rank
            ))
        self.finish(json.dumps(ret))


class VoteRoleHandler(BaseHandler):
    """
    投票规则
    """
    SUPPORTED_METHODS = ('GET', 'OPTIONS')

    async def get(self, pk, *args, **kwargs):
        vote = await objects.get(Vote, id=pk)

        self.finish({'detail': vote.rules})


class VotingHandler(BaseHandler):
    """
    投票接口
    """
    SUPPORTED_METHODS = ('POST', 'OPTIONS')

    @async_authenticated
    async def post(self, *args, **kwargs):
        candidate_id = self.get_json_argument('candidate_id')
        candidate_id = candidate_id[0].decode()
        try:
            async with objects.database.atomic_async():
                candidate = await objects.get(Candidate, id=candidate_id)
                key = f'vote_user_{self.current_user.id}_date_{date.today()}'
                is_vote = redis.get(key)
                if is_vote:
                    raise IsVoteError
                await objects.create(VoteEvent,
                                     vote_id=candidate.vote_id,
                                     voter_id=self.current_user.id,
                                     voter_avatar=self.current_user.avatar,
                                     voter_nickname=self.current_user.nickname,
                                     candidate_id=candidate.id,
                                     reach=1)
                candidate.number_of_votes += 1
                await objects.update(candidate)
                redis.set(key, '1', 24 * 60 * 60)
            self.finish(json.dumps({'number_of_votes': candidate.number_of_votes}))
        except Candidate.DoesNotExist:
            raise NotFoundError("参赛选手未找到")
