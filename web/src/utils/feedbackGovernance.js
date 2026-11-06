export const feedbackStatusName = value => ({OPEN:'待指派', ASSIGNED:'已指派', PROCESSING:'处理中', IN_REVIEW:'待复核', RESOLVED:'复核通过', CLOSED:'已关闭'})[value] || value
