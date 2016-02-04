import sys
import time
import numpy as np
from datetime import datetime
from pymongo.errors import DuplicateKeyError, ServerSelectionTimeoutError
import pymongo
import logging
import time
import datetime
import itertools
from riotwatcher import LoLException
from .extract_match import extract_match_infos

logger = logging.getLogger(__name__)

logger.setLevel(logging.DEBUG)

# create a file handler
handler = logging.FileHandler('crawler.log')
handler.setLevel(logging.DEBUG)

# create a logging format
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)

logger.addHandler(handler)


MATCHLIST_COLLECTION = "matchlist"
MATCH_COLLECTION = "match"
MATCHLIST_PAGE_LIMIT = 60


## TODO: Crawling of RANKED_TEAM_5x5 in ChallengerLolCrawler
## TODO: Use riotwatcher: use constants
## TODO: Extend crawl.py script with new Crawler
## TODO: Maybe create a method in rito.py to have the pagination and MATCHLIST_PAGE_LIMIT
##       in the API logic
## TODO: Remove crawl-top-matches.py again
## TODO: When getting matchlists, check if mathlist already in db. if so, check maximum timestamp and set as begin_time
## TODO: Write some tests
## OPTIONAL TODO: Create own extract module
## OPTIONAL TODO: Create constants module
## OPTIONAL TODO: Extraction should work like aggregate.py
## OPTIONAL TODO: Parallel processing in TopCrawler
## OPTIONAL TODO: Threads in lolcrawler


class LolCrawlerBase():
    """Crawler base class for all crawlers"""

    def __init__(self, api, db_client, include_timeline=False):
        self.api = api
        self.region = api.default_region
        self.include_timeline = include_timeline
        ## Stack of summoner ids to crawl
        self.summoner_ids = []
        self.summoner_ids_done = []
        ## Stack of match ids to crawl
        self.match_ids = []
        self.match_ids_done = []
        self.db_client = db_client


    def _store(self, identifier, entity_type, entity, upsert=False):
        """Stores matches and matchlists"""
        entity.update({'_id': identifier})
        try:
            if upsert:
                self.db_client[entity_type].replace_one(filter={"_id": identifier}, replacement = entity, upsert=True)
            else:
                self.db_client[entity_type].insert_one(entity)
        except DuplicateKeyError:
            logger.warning("Duplicate: Mongodb already inserted {entity_type} with id {identifier}".format(entity_type=entity_type, identifier=identifier))
        except ServerSelectionTimeoutError as e:
            logger.error("Could not connect to Mongodb", exc_info=True)
            sys.exit(1)

    def flush(self):
        self.summoner_ids = []
        self.match_ids = []

    def crawl_matchlist(self, summoner_id, region=None,  **kwargs):
        """Crawls matchlist of given summoner,
        stores it and saves the matchIds"""
        logger.debug('Getting partial matchlist of summoner %s' % (summoner_id))
        wait_for_api(self.api)
        matchlist = self.api.get_match_list(summoner_id, region=region,  **kwargs)
        matchlist["extractions"] = {"region": self.region}
        self._store(identifier=summoner_id, entity_type=MATCHLIST_COLLECTION, entity=matchlist, upsert=True)
        self.summoner_ids_done.append(summoner_id)
        match_ids = [x['matchId'] for x in matchlist['matches']]
        self.match_ids.extend(match_ids)
        return match_ids

    def crawl_complete_matchlist(self, summoner_id,  region=None,  **kwargs):
        """Crawls complete matchlist by going through paginated matchlists of given summoner,
        stores it and saves the matchIds"""

        logger.debug('Getting complete matchlist of summoner %s' % (summoner_id))
        more_matches=True
        ## Start with empty matchlist
        matchlist={"matches": [], "totalGames": 0}
        begin_index=0
        while more_matches:
            wait_for_api(self.api)
            new_matchlist = self.api.get_match_list(summoner_id=summoner_id,
                                                    begin_index=begin_index,
                                                    end_index=begin_index + MATCHLIST_PAGE_LIMIT,
                                                    region=region,  **kwargs)
            if "matches" in new_matchlist.keys():
                matchlist["matches"] = matchlist["matches"] + new_matchlist["matches"]
                matchlist["totalGames"] = matchlist["totalGames"] + new_matchlist["totalGames"]
                begin_index += MATCHLIST_PAGE_LIMIT
            else:
                more_matches=False

        region = region if region else self.region
        matchlist["extractions"] = {"region": region}
        self._store(identifier=summoner_id, entity_type=MATCHLIST_COLLECTION, entity=matchlist, upsert=True)
        self.summoner_ids_done.append(summoner_id)
        match_ids = [x['matchId'] for x in matchlist['matches']]
        self.match_ids.extend(match_ids)
        return match_ids


    def crawl_match(self, match_id, region=None):
        """Crawl match with given match_id,
        stores it and saves the matchID"""
        ## Check if match is in database and only crawl if not in database
        match_in_db = self.db_client[MATCH_COLLECTION].find({"_id": match_id})
        if match_in_db.count() == 0:
            logger.debug('Crawling match %s' % (match_id))
            wait_for_api(self.api)
            match = self.api.get_match(match_id=match_id, include_timeline=self.include_timeline, region=region)
            match["extractions"] = extract_match_infos(match)
            self._store(identifier=match_id, entity_type=MATCH_COLLECTION, entity=match)
            try:
                summoner_ids = [x['player']['summonerId'] for x in match['participantIdentities']]
                ## remove summoner ids the crawler has already seen
                new_summoner_ids = list(set(summoner_ids) - set(self.summoner_ids_done))
                self.summoner_ids = new_summoner_ids + self.summoner_ids
            except:
                logger.error('Could not find participant data in match with id %s' % (match_id))
        else:
            logger.debug("Skipping match with matchId %s. Already in DB" % (match_id))






class LolCrawler(LolCrawlerBase):
    """Randomly crawls matches starting from seed summoner"""

    def start(self, start_summoner_id):
        """Start infinite crawling loop"""
        logger.info("Start crawling")
        last_summoner_cursor = self.db_client[MATCHLIST_COLLECTION].find({"extractions.region": self.region}).sort("$natural", pymongo.DESCENDING)
        if last_summoner_cursor.count() == 0:
            self.summoner_ids = [start_summoner_id]
            logger.info("No summoner ids found in database, starting with seed summoner")
        else:
            for i in range(0, 100):
                self.summoner_ids += [last_summoner_cursor.next()["_id"]]
            logger.info("Starting with latest summoner ids in database")
        while True:
            self.crawl()


    def crawl(self):
        summoner_id = self.summoner_ids.pop()
        logger.info("Crawling summoner {summoner_id}".format(summoner_id=summoner_id))

        try:
            match_ids = self.crawl_matchlist(summoner_id)
            ## Choose from last ten matches
            random_match_id = np.random.choice(range(0, min(10, len(match_ids))))
            match_id = match_ids[random_match_id]
            self.crawl_match(match_id)
        except LoLException as e:
            logger.error(e)
            self.crawl()




class TopLolCrawler(LolCrawler):
    """Crawl all matches from all challengers"""

    def crawl(self, region, league, season):
        '''Crawl all matches from players in given region, league and season'''
        logger.info('Crawling matches for %sp layers in %s, season %s' % (league, region, season))
        ## Add ids of solo q top summoners to self.summmone_ids
        self._get_top_summoner_ids(region, league, season)
        ## Get all summoner ids of the league
        logger.info("Crawling matchlists of %i players" % (len(self.summoner_ids)))
        ## Get matchlists for all summoners
        self._get_top_summoner_matchlists(region, league, season)
        logger.info("Crawling %i matches" %(len(self.match_ids)))
        self._get_top_summoners_matches(region)
        self.flush()
        return None

    def _get_top_summoner_ids(self, region, league, season):
        queue = "RANKED_SOLO_5x5"
        wait_for_api(self.api)
        if league == 'challenger':
            league_list = self.api.get_challenger(region=region, queue=queue)
        elif league == 'master':
            league_list = self.api.get_master(region=region, queue=queue)
        self.summoner_ids = [x["playerOrTeamId"] for x in league_list["entries"]]
        return None

    # def _get_top_teams(region, league, season):
    #     pass


    def _get_top_summoner_matchlists(self, region, league, season):
        '''Download and store matchlists for self.summoner_ids'''
        for summoner_id in self.summoner_ids:
            try:
                self.crawl_complete_matchlist(summoner_id=summoner_id,
                                              region=region,
                                              begin_time=self.begin_time,
                                              end_time=self.end_time,
                                              season=season)
            except LoLException as e:
                logger.error(e)
        return None


    def _get_top_summoners_matches(self, region):
        for match_id in self.match_ids:
            try:
                self.crawl_match(match_id, region=region)
            except LoLException as e:
                logger.error(e)
        return None

    def start(self,
              begin_time,
              regions=['euw', 'eune', 'kr', 'na'],
              end_time = int(time.time() * 1000),
              leagues=['challenger', 'master'],
              seasons=["SEASON2016"]
              ):

        self.begin_time = int(begin_time.strftime('%s')) * 1000
        self.end_time = end_time

        for region, league, season in itertools.product(regions, leagues, seasons):
            self.crawl(region=region, league=league, season=season)

def wait_for_api(api):
    while not api.can_make_request:
        logger.info('Reached API limit, waiting 0.1 seconds')
        sys.Sleep(0.1)
    return None
